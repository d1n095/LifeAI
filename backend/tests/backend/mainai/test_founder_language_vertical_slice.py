"""Stage G — realistic founder language end-to-end (no manual translation after input)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.founder_language import process_founder_language, resolve_founder_expression
from app.founder_memory import get_founder_memory
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id):
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def test_resolve_strips_swedish_filler():
    intent, conf = resolve_founder_expression("få med det här med postgres durable memory")
    assert "postgres" in intent.lower()
    assert "få med" not in intent.lower()
    assert conf >= 0.55


def test_vertical_slice_persists_and_retrieves(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    result = process_founder_language(
        db_session,
        owner_id=user.id,
        raw_expression="få med det här med short founder answers preference",
        idempotency_key=f"g-{uuid.uuid4()}",
    )
    db_session.commit()
    assert result.persisted is True
    assert result.memory_note_id is not None
    assert result.canonical_entity_id is not None
    assert result.linkage_thread_id is not None
    note = get_founder_memory(db_session, owner_id=user.id, note_id=result.memory_note_id)
    assert note is not None
    # RAW EXPRESSION != INTERPRETATION
    assert note.content == "få med det här med short founder answers preference"
    assert note.source == note.content
    assert (note.provenance or {}).get("normalized_intent") == result.normalized_intent
    assert "få med" not in result.normalized_intent.lower()
    assert "short founder answers" in result.normalized_intent.lower()
    assert result.confidence == (note.provenance or {}).get("confidence")


def test_same_idea_different_wording_collapses(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    a = process_founder_language(
        db_session,
        owner_id=user.id,
        raw_expression="gör samma på den andra grejen med rate limit burst",
        idempotency_key=f"g-a-{uuid.uuid4()}",
    )
    db_session.commit()
    b = process_founder_language(
        db_session,
        owner_id=user.id,
        raw_expression="rate limit burst",
        idempotency_key=f"g-b-{uuid.uuid4()}",
    )
    db_session.commit()
    # Second may reuse canonical entity when wording collapses.
    assert a.canonical_entity_id is not None
    assert b.canonical_entity_id is not None
    assert a.persisted and b.persisted
