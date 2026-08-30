"""Stage P — cognitive load reduction."""

from __future__ import annotations

import uuid

from app.cognitive_load import consider_founder_question
from app.inspectable_memory import founder_add_memory_note
from app.models.user import User


def _owner(db):
    user = User(email=f"p-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_stage_p_avoids_unnecessary_question(superuser_db):
    owner = _owner(superuser_db)
    founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Use Postgres for sessions", note_type="decision", idempotency_key="p1"
    )
    superuser_db.commit()
    decision = consider_founder_question(superuser_db, owner_id=owner.id, question="what about postgres sessions?")
    superuser_db.commit()
    assert decision.avoided_question is True
    assert decision.should_ask_founder is False
    assert decision.metrics["unnecessary_questions_avoided"] >= 1


def test_stage_p_surfaces_consequential(superuser_db):
    owner = _owner(superuser_db)
    decision = consider_founder_question(
        superuser_db, owner_id=owner.id, question="ska vi deploy till production nu?"
    )
    assert decision.should_ask_founder is True


def test_current_truth_prefers_correction_over_old_note(superuser_db):
    """Old A → correction B → B is current; A never selected as current."""
    from app.inspectable_memory import founder_correct_memory_note
    from app.founder_memory import get_founder_memory

    owner = _owner(superuser_db)
    note_a, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Use Mongo for sessions storage",
        note_type="decision",
        idempotency_key="p-old-a",
    )
    superuser_db.commit()
    note_b, _ = founder_correct_memory_note(
        superuser_db,
        owner_id=owner.id,
        note_id=note_a.id,
        content="Use Postgres for sessions storage",
        idempotency_key="p-corr-b",
    )
    superuser_db.commit()
    old = get_founder_memory(superuser_db, owner_id=owner.id, note_id=note_a.id)
    assert old is not None
    assert old.status == "superseded"

    decision = consider_founder_question(
        superuser_db, owner_id=owner.id, question="what about sessions storage database?"
    )
    superuser_db.commit()
    assert decision.should_ask_founder is False
    assert decision.inferred_answer == "Use Postgres for sessions storage"
    assert decision.evidence_refs[0]["id"] == str(note_b.id)
    assert "Mongo" not in (decision.inferred_answer or "")


def test_multiple_corrections_deterministic_current(superuser_db):
    from app.inspectable_memory import founder_correct_memory_note

    owner = _owner(superuser_db)
    a, _ = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Deploy target is staging only", note_type="decision", idempotency_key="p-m-a"
    )
    superuser_db.commit()
    b, _ = founder_correct_memory_note(
        superuser_db, owner_id=owner.id, note_id=a.id, content="Deploy target is canary", idempotency_key="p-m-b"
    )
    superuser_db.commit()
    c, _ = founder_correct_memory_note(
        superuser_db, owner_id=owner.id, note_id=b.id, content="Deploy target is production canary", idempotency_key="p-m-c"
    )
    superuser_db.commit()
    d1 = consider_founder_question(superuser_db, owner_id=owner.id, question="what is the canary target environment?")
    d2 = consider_founder_question(superuser_db, owner_id=owner.id, question="remind me canary target environment setup")
    assert d1.inferred_answer == "Deploy target is production canary"
    assert d2.inferred_answer == d1.inferred_answer
    assert d1.evidence_refs[0]["id"] == str(c.id)
