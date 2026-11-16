"""app.mainai_executive.kill_criteria -- deterministic, read-only kill-criteria evaluation
over real WorkCandidate/LifeIntent/EngineeringLesson fields. SUNK COST != CONTINUE.

The mandatory "never mutates" proof: a structural source-inspection test that fails if a
future edit adds a db.add/db.commit/UPDATE anywhere in this module, plus a behavioral test
that the database row is byte-for-byte unchanged after calling evaluate_kill_criteria()."""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.life_intents.service import create_intent
from app.mainai_executive.kill_criteria import DEFAULT_STALE_AGE_DAYS, evaluate_kill_criteria
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonConfidence, EngineeringLessonSeverity, EngineeringLessonStatus
from app.models.user import User
from app.models.work_candidate import WorkCandidate
from app.work_candidates.service import dismiss_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _owner(db) -> User:
    user = User(email=f"kill-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _lesson(db, *, status=EngineeringLessonStatus.disputed, applies_to=None):
    lesson = EngineeringLesson(
        status=status, problem="p", root_cause="r", affected_component="wip", severity=EngineeringLessonSeverity.low,
        evidence="e", fix="f", general_rule="g", applies_to=applies_to or ["kill-test-tag"],
        source_type="test", source_ref="test", first_seen_at=datetime.utcnow(), created_by="test",
        confidence=EngineeringLessonConfidence.likely,
    )
    db.add(lesson)
    db.flush()
    return lesson


def test_evaluate_kill_criteria_requires_at_least_one_target(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    with pytest.raises(ValueError):
        evaluate_kill_criteria(superuser_db, owner_id=owner.id)


def test_dismissed_work_candidate_is_already_kill(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    from app.work_candidates.service import record_work_candidate

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="dead idea",
        idempotency_key=f"kill-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    dismiss_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, reason="not worth it")
    superuser_db.commit()

    result = evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert result["should_kill"] is True
    assert result["confidence"] >= 0.9
    assert any("dismissed" in r for r in result["reasons"])


def test_live_unreviewed_work_candidate_is_not_kill(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    from app.work_candidates.service import record_work_candidate

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="fresh idea",
        idempotency_key=f"kill-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()

    result = evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert result["should_kill"] is False


def test_stale_unreviewed_work_candidate_flagged(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    from app.work_candidates.service import record_work_candidate

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="old idea",
        idempotency_key=f"kill-{uuid.uuid4()}", classifier_strategy="test",
    )
    candidate.observed_at = datetime.utcnow() - timedelta(days=DEFAULT_STALE_AGE_DAYS + 5)
    superuser_db.commit()

    result = evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert result["should_kill"] is True
    assert any("unreviewed for over" in r for r in result["reasons"])


def test_disputed_lesson_tag_overlap_is_soft_signal(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    from app.work_candidates.service import record_work_candidate

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="tagged idea",
        idempotency_key=f"kill-{uuid.uuid4()}", classifier_strategy="test",
        provenance={"tags": ["kill-test-tag"]},
    )
    _lesson(superuser_db, status=EngineeringLessonStatus.disputed, applies_to=["kill-test-tag"])
    superuser_db.commit()

    result = evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    wc = result["work_candidate"]
    assert any("disputed EngineeringLesson" in r for r in wc["reasons"])


def test_abandoned_life_intent_is_already_kill(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = create_intent(superuser_db, owner_id=owner.id, title="dead intent", state="abandoned", idempotency_key=f"ki-{uuid.uuid4()}")
    superuser_db.commit()

    result = evaluate_kill_criteria(superuser_db, owner_id=owner.id, life_intent_id=intent.id)
    assert result["should_kill"] is True
    assert any("state is already" in r for r in result["reasons"])


def test_completed_life_intent_is_not_kill():
    """completed is a SUCCESS terminal state, not a kill -- must never be conflated."""
    from app.mainai_executive.kill_criteria import _LIFE_INTENT_KILLED_STATES

    assert "completed" not in _LIFE_INTENT_KILLED_STATES


def test_active_life_intent_not_stale_is_not_kill(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    intent = create_intent(superuser_db, owner_id=owner.id, title="fresh intent", state="active", idempotency_key=f"ki-{uuid.uuid4()}")
    superuser_db.commit()

    result = evaluate_kill_criteria(superuser_db, owner_id=owner.id, life_intent_id=intent.id)
    assert result["should_kill"] is False


def test_evaluate_kill_criteria_never_mutates_the_row(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    from app.work_candidates.service import record_work_candidate

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="untouched",
        idempotency_key=f"kill-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    before_updated_at = candidate.updated_at

    evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert superuser_db.new == set() or len(superuser_db.new) == 0
    assert superuser_db.dirty == set() or len(superuser_db.dirty) == 0
    superuser_db.expire_all()
    reloaded = superuser_db.execute(select(WorkCandidate).where(WorkCandidate.id == candidate.id)).scalar_one()
    assert reloaded.status == "unreviewed"
    assert reloaded.updated_at == before_updated_at


def test_kill_criteria_module_has_no_mutating_call_anywhere():
    """Structural: no db.add/db.commit/UPDATE literal appears anywhere in this module's
    source -- proof this is advisory-only by construction, not merely by this run's behavior."""
    import app.mainai_executive.kill_criteria as module

    source = inspect.getsource(module)
    assert not re.search(r"\bdb\.add\(", source)
    assert not re.search(r"\bdb\.commit\(", source)
    assert not re.search(r"\bdb\.flush\(", source)
    # A real raw-SQL UPDATE keyword (all-caps, standalone) -- deliberately case-SENSITIVE and
    # requiring a following space, so this does not false-positive on harmless identifiers
    # like `tags.update(...)` (a Python set method) or `row.updated_at` (a column READ).
    assert not re.search(r"\bUPDATE \w", source)
    assert not re.search(r"row\.\w+\s*=[^=]", source), "no attribute assignment on any fetched row"
