"""MainAI Cognitive Control Plane -- `app.mainai_vision.mind_change` -- proves the "What Changes
My Mind" ledger is durable (RESTART PERSISTENCE: a fresh load after save survives, cross-session,
same mechanism as `session_checkpoint.py`'s own proven precedent), append-only (a later save
supersedes, never mutates), and owner-scoped.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_vision.evidence import MindChangeRecord
from app.mainai_vision.mind_change import load_mind_change_record, save_mind_change_record
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"mc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


def _record(conclusion="MainAI should reuse existing project_entities", confidence=0.7) -> MindChangeRecord:
    return MindChangeRecord(
        conclusion=conclusion, confidence=confidence, support=("existing table already RLS-scoped",),
        contradictions=(), assumptions=("entity_type CHECK can be safely widened",), unknowns=("real usage volume",),
        what_strengthens=("a real caller successfully compiles a graph",),
        what_weakens=("a real caller needs a competing shape",),
        what_reverses=("project_entities is deprecated",),
    )


def test_load_before_any_save_is_none_not_an_error(superuser_db, owner_id):
    assert load_mind_change_record(superuser_db, owner_id=owner_id, conclusion="never saved") is None


def test_save_then_load_round_trips_through_real_founder_memory(superuser_db, owner_id):
    record = _record()
    save_mind_change_record(superuser_db, owner_id=owner_id, record=record)
    superuser_db.commit()

    loaded = load_mind_change_record(superuser_db, owner_id=owner_id, conclusion=record.conclusion)
    assert loaded is not None
    assert loaded.conclusion == record.conclusion
    assert loaded.confidence == record.confidence
    assert loaded.support == record.support
    assert loaded.what_reverses == record.what_reverses


def test_second_save_supersedes_the_first_via_supersedes_note_id_chain(superuser_db, owner_id):
    first = save_mind_change_record(superuser_db, owner_id=owner_id, record=_record(confidence=0.5))
    superuser_db.commit()
    second = save_mind_change_record(superuser_db, owner_id=owner_id, record=_record(confidence=0.9))
    superuser_db.commit()

    superuser_db.refresh(first)
    assert first.status == "superseded"
    assert second.provenance["record"]["confidence"] == 0.9

    loaded = load_mind_change_record(superuser_db, owner_id=owner_id, conclusion=_record().conclusion)
    assert loaded.confidence == 0.9


def test_owner_isolation(superuser_db, owner_id):
    other = User(email=f"mc-other-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(other)
    superuser_db.flush()
    superuser_db.commit()

    save_mind_change_record(superuser_db, owner_id=owner_id, record=_record())
    superuser_db.commit()

    assert load_mind_change_record(superuser_db, owner_id=other.id, conclusion=_record().conclusion) is None
