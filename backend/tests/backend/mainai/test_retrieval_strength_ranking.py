"""app.mainai_executive.retrieval -- pure post-processing re-rank over app.active_context's
real current_members() output. Never queries the database; never touches active_context/
service.py itself."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.active_context.service import create_context_set, current_members, refresh_context
from app.mainai_executive.retrieval import StrengthWeights, rank_by_strength
from app.models.founder_memory import FounderMemoryNote
from app.models.user import User


def _owner(db) -> User:
    user = User(email=f"retr-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _note(db, owner_id, *, content="n"):
    note = FounderMemoryNote(owner_id=owner_id, note_type="decision", content=content, idempotency_key=f"n-{uuid.uuid4()}")
    db.add(note)
    db.flush()
    return note


def test_rank_by_strength_is_pure_and_does_not_mutate(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    note = _note(superuser_db, owner.id)
    superuser_db.commit()
    context = create_context_set(
        superuser_db, owner_id=owner.id, anchor_type="founder_memory_note", anchor_ref=str(note.id),
        idempotency_key=f"ctx-{uuid.uuid4()}",
    )
    refresh_context(superuser_db, owner_id=owner.id, context_set_id=context.id)
    superuser_db.commit()
    members = current_members(superuser_db, owner_id=owner.id, context_set_id=context.id)

    ranked = rank_by_strength(members)
    assert superuser_db.new == set() or len(superuser_db.new) == 0
    assert superuser_db.dirty == set() or len(superuser_db.dirty) == 0
    assert len(ranked) == len(members)
    assert all(r.object_type == "founder_memory_note" for r in ranked)


def test_pinned_member_always_sorts_first_regardless_of_score():
    now = datetime.utcnow()

    class _Row:
        def __init__(self, object_type, object_ref, state, last_activated_at):
            self.object_type = object_type
            self.object_ref = object_ref
            self.state = state
            self.last_activated_at = last_activated_at
            self.added_at = last_activated_at

    items = [
        _Row("work_candidate", "a", "active", now),  # will get high confidence
        _Row("work_candidate", "b", "pinned", now - timedelta(days=365)),  # old, low confidence, but pinned
    ]
    ranked = rank_by_strength(
        items,
        confidence_by_key={("work_candidate", "a"): 1.0, ("work_candidate", "b"): 0.0},
    )
    assert ranked[0].object_ref == "b"
    assert ranked[0].pinned is True


def test_higher_confidence_ranks_above_lower_confidence_all_else_equal():
    now = datetime.utcnow()

    class _Row:
        def __init__(self, ref):
            self.object_type = "work_candidate"
            self.object_ref = ref
            self.state = "active"
            self.last_activated_at = now
            self.added_at = now

    items = [_Row("low"), _Row("high")]
    ranked = rank_by_strength(
        items, confidence_by_key={("work_candidate", "low"): 0.1, ("work_candidate", "high"): 0.9}
    )
    assert [r.object_ref for r in ranked] == ["high", "low"]


def test_more_recent_item_ranks_above_older_all_else_equal():
    now = datetime.utcnow()

    class _Row:
        def __init__(self, ref, age_days):
            self.object_type = "work_candidate"
            self.object_ref = ref
            self.state = "active"
            self.last_activated_at = now - timedelta(days=age_days)
            self.added_at = self.last_activated_at

    items = [_Row("old", 300), _Row("new", 0)]
    ranked = rank_by_strength(items)
    assert [r.object_ref for r in ranked] == ["new", "old"]


def test_more_contradictions_ranks_lower_all_else_equal():
    now = datetime.utcnow()

    class _Row:
        def __init__(self, ref):
            self.object_type = "work_candidate"
            self.object_ref = ref
            self.state = "active"
            self.last_activated_at = now
            self.added_at = now

    items = [_Row("disputed"), _Row("clean")]
    ranked = rank_by_strength(
        items, contradiction_count_by_key={("work_candidate", "disputed"): 5, ("work_candidate", "clean"): 0}
    )
    assert [r.object_ref for r in ranked] == ["clean", "disputed"]


def test_missing_confidence_falls_back_to_neutral_default():
    now = datetime.utcnow()

    class _Row:
        object_type = "work_candidate"
        object_ref = "unknown-conf"
        state = "active"
        last_activated_at = now
        added_at = now

    ranked = rank_by_strength([_Row()])
    assert ranked[0].components["confidence"] == 0.5


def test_strength_weights_reject_nonpositive_sum():
    import pytest

    with pytest.raises(ValueError):
        StrengthWeights(confidence=0.0, recency=0.0, contradiction=0.0)


def test_empty_items_returns_empty_list():
    assert rank_by_strength([]) == []
