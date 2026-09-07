"""app.mainai_executive.rejected_idea_guard -- deterministic (no LLM) tag/title-overlap check
against recently rejected IntelligenceIdea/WorkCandidate rows. Never blocks by itself."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.intelligence_governance.service import record_execution, record_idea
from app.mainai_executive.rejected_idea_guard import check_recently_rejected
from app.models.intelligence_governance import IntelligenceIdea
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User
from app.work_candidates.service import dismiss_work_candidate, record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _owner(db) -> User:
    user = User(email=f"guard-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="p", original_instruction="s", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit")
    db.add(task)
    db.flush()
    return task


def test_no_signal_when_nothing_recently_rejected(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    result = check_recently_rejected(superuser_db, owner_id=owner.id, candidate_title="Migrate database to Postgres")
    assert result["is_likely_duplicate_of_rejected"] is False
    assert result["matches"] == []
    assert result["blocks"] is False


def test_detects_overlap_with_rejected_idea(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="e1", role="builder")
    record_idea(
        superuser_db, owner_id=owner.id, execution_id=execution.id, idea_kind="idea",
        content="Migrate the entire database over to Postgres for better performance",
        disposition="rejected", disposition_reason="too risky right now", idempotency_key="i1",
    )
    superuser_db.commit()

    result = check_recently_rejected(superuser_db, owner_id=owner.id, candidate_title="Migrate database to Postgres for performance")
    assert result["is_likely_duplicate_of_rejected"] is True
    assert result["matches"][0]["kind"] == "intelligence_idea"
    assert result["blocks"] is False


def test_detects_overlap_with_dismissed_work_candidate(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id,
        title="Rewrite the billing subsystem in Rust", idempotency_key=f"g-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    dismiss_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, reason="not now")
    superuser_db.commit()

    result = check_recently_rejected(superuser_db, owner_id=owner.id, candidate_title="Rewrite billing subsystem in Rust")
    assert result["is_likely_duplicate_of_rejected"] is True
    assert result["matches"][0]["kind"] == "work_candidate"
    assert result["matches"][0]["status"] == "dismissed"


def test_unrelated_title_does_not_match(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="e2", role="builder")
    record_idea(
        superuser_db, owner_id=owner.id, execution_id=execution.id, idea_kind="idea",
        content="Add dark mode toggle to settings page",
        disposition="rejected", disposition_reason="not a priority", idempotency_key="i2",
    )
    superuser_db.commit()

    result = check_recently_rejected(superuser_db, owner_id=owner.id, candidate_title="Fix a memory leak in the export worker")
    assert result["is_likely_duplicate_of_rejected"] is False


def test_lookback_window_excludes_old_rejections(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="e3", role="builder")
    # intelligence_ideas is DB-enforced append-only (migration 0038's own deny-mutation
    # trigger) -- an old created_at must be set at INSERT time, never via a post-commit
    # attribute mutation (which would itself be a rejected UPDATE).
    idea = IntelligenceIdea(
        owner_id=owner.id, execution_id=execution.id, idea_kind="idea",
        content="Switch the queue backend to Kafka entirely",
        disposition="rejected", disposition_reason="too complex", idempotency_key="i3",
        created_at=datetime.utcnow() - timedelta(days=200),
    )
    superuser_db.add(idea)
    superuser_db.commit()

    result = check_recently_rejected(
        superuser_db, owner_id=owner.id, candidate_title="Switch queue backend to Kafka", lookback_days=90
    )
    assert result["is_likely_duplicate_of_rejected"] is False


def test_never_blocks_by_construction(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="e4", role="builder")
    record_idea(
        superuser_db, owner_id=owner.id, execution_id=execution.id, idea_kind="idea",
        content="identical duplicate idea content for overlap test",
        disposition="rejected", disposition_reason="no", idempotency_key="i4",
    )
    superuser_db.commit()
    result = check_recently_rejected(
        superuser_db, owner_id=owner.id, candidate_title="identical duplicate idea content for overlap test"
    )
    assert result["is_likely_duplicate_of_rejected"] is True
    assert result["blocks"] is False  # signal only -- caller decides
