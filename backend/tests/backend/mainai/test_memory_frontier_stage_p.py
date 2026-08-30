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
