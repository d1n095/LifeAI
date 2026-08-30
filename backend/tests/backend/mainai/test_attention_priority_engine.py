"""Stage M — attention ranking tests."""

from __future__ import annotations

import uuid

from app.attention_engine import rank_attention
from app.inspectable_memory import founder_add_memory_note
from app.models.user import User


def test_rank_prefers_now_decisions(superuser_db):
    owner = User(email=f"att-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Ship attention ranking now", note_type="decision", idempotency_key="m1"
    )
    founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Multi-region someday long-term", note_type="goal", idempotency_key="m2"
    )
    superuser_db.commit()
    ranked = rank_attention(superuser_db, owner_id=owner.id, founder_goal_text="Ship attention")
    assert ranked
    assert all(i.authority_implied is False for i in ranked)
    assert ranked[0].score >= ranked[-1].score
    assert any(i.horizon == "now" for i in ranked)
