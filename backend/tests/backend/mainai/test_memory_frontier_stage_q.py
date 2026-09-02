"""Stage Q — interruption recovery from durable state only."""

from __future__ import annotations

import uuid

from app.inspectable_memory import founder_add_memory_note
from app.interruption_recovery import recover_after_interruption
from app.models.user import User


def test_stage_q_interruption_recovery_durable_only(superuser_db):
    owner = User(email=f"q-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Continue memory frontier", note_type="goal", idempotency_key="q1"
    )
    superuser_db.commit()
    brief = recover_after_interruption(superuser_db, owner_id=owner.id, gap="days")
    assert brief.durable_only is True
    assert brief.best_next_action
