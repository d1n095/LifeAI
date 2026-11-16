"""app.mainai_executive.architectural_gravity -- deterministic, read-only detection of the SAME
underlying pattern recurring across multiple INDEPENDENT memory_threads. The opposite direction
from missing_piece.detect_missing_pieces(). Don't-spam bar: N unrelated items produce no
signal; N genuinely related items (same tag across independent threads) do."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.mainai_executive.architectural_gravity import detect_architectural_gravity
from app.memory_threads.service import add_member, create_thread
from app.models.memory_thread import MemoryThread
from app.models.user import User
from app.work_candidates.service import record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _thread_with_candidate_tag(db, *, owner_id, entity_id, tag: str) -> MemoryThread:
    candidate = record_work_candidate(
        db, owner_id=owner_id, source_entity_id=entity_id, title=f"candidate for {tag}",
        idempotency_key=f"grav-wc-{uuid.uuid4()}", classifier_strategy="test",
        provenance={"tags": [tag]},
    )
    thread = create_thread(db, owner_id=owner_id, idempotency_key=f"grav-thread-{uuid.uuid4()}")
    add_member(
        db, owner_id=owner_id, thread_id=thread.id, member_kind="work_candidate",
        member_ref_id=candidate.id, actor_type="system",
    )
    return thread


def test_no_gravity_from_unrelated_items(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    for tag in ("alpha-unrelated", "bravo-unrelated", "charlie-unrelated"):
        _thread_with_candidate_tag(superuser_db, owner_id=owner.id, entity_id=entity.id, tag=tag)
    superuser_db.commit()

    result = detect_architectural_gravity(superuser_db, owner_id=owner.id, min_independent_threads=3)
    assert result["gravity_detected"] is False
    assert result["candidates"] == []
    assert result["auto_created"] is False
    assert result["authority_impact"] == "NONE"


def test_gravity_detected_for_same_tag_across_independent_threads(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    shared_tag = f"shared-pattern-{uuid.uuid4().hex[:8]}"
    thread_ids = set()
    for _ in range(3):
        thread = _thread_with_candidate_tag(superuser_db, owner_id=owner.id, entity_id=entity.id, tag=shared_tag)
        thread_ids.add(str(thread.id))
    superuser_db.commit()

    result = detect_architectural_gravity(superuser_db, owner_id=owner.id, min_independent_threads=3)
    assert result["gravity_detected"] is True
    matching = [c for c in result["candidates"] if c["tag"] == shared_tag]
    assert len(matching) == 1
    assert matching[0]["independent_thread_count"] == 3
    assert set(matching[0]["thread_ids"]) == thread_ids
    assert result["auto_created"] is False  # proposes only, never auto-creates


def test_gravity_requires_independent_threads_not_one_thread_with_many_members(superuser_db):
    """Multiple items sharing a tag inside the SAME thread is not new signal -- gravity is
    about the pattern recurring ACROSS independent threads."""
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    shared_tag = f"single-thread-pattern-{uuid.uuid4().hex[:8]}"
    thread = create_thread(superuser_db, owner_id=owner.id, idempotency_key=f"grav-onethread-{uuid.uuid4()}")
    for _ in range(4):
        candidate = record_work_candidate(
            superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="same-thread candidate",
            idempotency_key=f"grav-wc-{uuid.uuid4()}", classifier_strategy="test",
            provenance={"tags": [shared_tag]},
        )
        add_member(
            superuser_db, owner_id=owner.id, thread_id=thread.id, member_kind="work_candidate",
            member_ref_id=candidate.id, actor_type="system",
        )
    superuser_db.commit()

    result = detect_architectural_gravity(superuser_db, owner_id=owner.id, min_independent_threads=3)
    assert result["gravity_detected"] is False


def test_lookback_window_excludes_stale_threads(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    shared_tag = f"stale-pattern-{uuid.uuid4().hex[:8]}"
    threads = [
        _thread_with_candidate_tag(superuser_db, owner_id=owner.id, entity_id=entity.id, tag=shared_tag)
        for _ in range(3)
    ]
    superuser_db.commit()
    # Push two threads' last_activity_at outside the lookback window.
    for thread in threads[:2]:
        thread.last_activity_at = datetime.utcnow() - timedelta(days=200)
    superuser_db.commit()

    result = detect_architectural_gravity(
        superuser_db, owner_id=owner.id, min_independent_threads=3, lookback_days=90
    )
    assert result["gravity_detected"] is False  # only 1 thread left inside the window


def test_only_this_owners_threads_are_considered(superuser_db):
    owner_a, entity_a = _owner_with_entity(superuser_db)
    owner_b, entity_b = _owner_with_entity(superuser_db)
    superuser_db.commit()
    shared_tag = f"cross-owner-{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        _thread_with_candidate_tag(superuser_db, owner_id=owner_a.id, entity_id=entity_a.id, tag=shared_tag)
    _thread_with_candidate_tag(superuser_db, owner_id=owner_b.id, entity_id=entity_b.id, tag=shared_tag)
    superuser_db.commit()

    result_a = detect_architectural_gravity(superuser_db, owner_id=owner_a.id, min_independent_threads=3)
    result_b = detect_architectural_gravity(superuser_db, owner_id=owner_b.id, min_independent_threads=3)
    assert result_a["gravity_detected"] is True
    assert result_b["gravity_detected"] is False  # owner_b only has 1 thread with that tag


def test_rejects_invalid_bounds(superuser_db):
    owner = User(email=f"grav-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    superuser_db.commit()
    import pytest

    with pytest.raises(ValueError):
        detect_architectural_gravity(superuser_db, owner_id=owner.id, lookback_days=0)
    with pytest.raises(ValueError):
        detect_architectural_gravity(superuser_db, owner_id=owner.id, min_independent_threads=1)
    with pytest.raises(ValueError):
        detect_architectural_gravity(superuser_db, owner_id=owner.id, limit=0)
