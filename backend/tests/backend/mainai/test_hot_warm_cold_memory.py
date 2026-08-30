"""Stage N — hot/warm/cold tier tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.inspectable_memory import founder_add_memory_note
from app.memory_tiers import MemoryTier, demote_stale, list_by_tier, record_retrieval, search_including_cold
from app.models.user import User


def test_retrieval_promotes_and_cold_remains_searchable(superuser_db):
    owner = User(email=f"tier-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    note, _ = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Historical lease takeover lesson", note_type="observation", idempotency_key="n1"
    )
    superuser_db.commit()
    # Force cold
    from app.memory_tiers.service import _get_or_create

    state = _get_or_create(superuser_db, owner_id=owner.id, target_kind="founder_memory_note", target_id=note.id)
    state.tier = MemoryTier.COLD.value
    superuser_db.flush()

    hits = search_including_cold(superuser_db, owner_id=owner.id, text="lease takeover")
    assert hits and hits[0]["truth_preserved"] is True
    assert hits[0]["tier"] == "cold"

    for _ in range(3):
        record_retrieval(superuser_db, owner_id=owner.id, target_kind="founder_memory_note", target_id=note.id)
    superuser_db.commit()
    hot = list_by_tier(superuser_db, owner_id=owner.id, tier=MemoryTier.HOT)
    assert any(r.target_id == note.id for r in hot)

    # Demote path
    state = hot[0]
    state.last_retrieved_at = datetime.utcnow() - timedelta(days=30)
    superuser_db.flush()
    changed = demote_stale(superuser_db, owner_id=owner.id, older_than_hours=24)
    superuser_db.commit()
    assert changed >= 1
