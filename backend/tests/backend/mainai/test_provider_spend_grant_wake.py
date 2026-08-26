"""Founder provider-spend grant must wake matching PROVIDER_SPEND_NOT_AUTHORIZED parks.

Proves: blocked park → authorize API/service → ready, without manual task mutation.
Wrong goal/owner/envelope/unrelated blockers stay blocked. Worker reconcile recovers
crash between grant commit and wake.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.provider_wait_wake import (
    PROVIDER_SPEND_PARK_BLOCKER,
    PROVIDER_SPEND_PARK_REASON,
    reconcile_provider_spend_parks_for_active_grants,
    wake_tasks_blocked_for_provider_spend,
)
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.user import User
from app.provider_spend import authorize_provider_spend, revoke_provider_spend


def _goal_with_ready_task(db, owner_id):
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="spend wake",
        original_instruction="edit README",
        created_by="test",
    )
    planner.create_plan(
        db,
        goal=goal,
        rationale="one",
        tasks=[PlannedTaskSpec(description="edit", task_type="repo_edit", risk_level="low")],
        created_by="test",
    )
    db.flush()
    return goal


def _envelope(db, owner_id, goal_id):
    proposal = propose_execution_scope(
        db, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"wake-prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"wake-env-{uuid.uuid4()}",
    )
    return envelope


def _park_spend(db, task: MainAITask) -> None:
    task.status = MainAITaskStatus.blocked
    task.blocker_reason = PROVIDER_SPEND_PARK_BLOCKER
    task.next_retry_at = None
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.blocked,
            detail={"reason": PROVIDER_SPEND_PARK_REASON, "phase": "PROVIDER_SPEND_NOT_AUTHORIZED"},
        )
    )
    db.flush()


def test_authorize_provider_spend_wakes_matching_parked_task(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    envelope = _envelope(superuser_db, owner.id, goal.id)
    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    _park_spend(superuser_db, task)
    superuser_db.commit()

    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=2,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"wake-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.ready
    assert task.blocker_reason is None


def test_authorize_does_not_wake_unrelated_or_wrong_goal_blocker(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal_a = _goal_with_ready_task(superuser_db, owner.id)
    goal_b = _goal_with_ready_task(superuser_db, owner.id)
    env_a = _envelope(superuser_db, owner.id, goal_a.id)
    _envelope(superuser_db, owner.id, goal_b.id)
    task_a = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal_a.id)).scalar_one()
    task_b = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal_b.id)).scalar_one()
    _park_spend(superuser_db, task_a)
    task_b.status = MainAITaskStatus.blocked
    task_b.blocker_reason = "Depends on task X, which is failed."
    db_event = MainAITaskEvent(
        task_id=task_b.id,
        owner_id=task_b.owner_id,
        event_type=MainAITaskEventType.blocked,
        detail={"reason": "autonomous_gap_repair_park"},
    )
    superuser_db.add(db_event)
    superuser_db.flush()
    superuser_db.commit()

    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal_a.id,
        execution_envelope_id=env_a.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=1,
        idempotency_key=f"wake-a-{uuid.uuid4()}",
    )
    superuser_db.commit()
    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    assert task_a.status == MainAITaskStatus.ready
    assert task_b.status == MainAITaskStatus.blocked


def test_revoked_or_expired_grant_does_not_wake(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    envelope = _envelope(superuser_db, owner.id, goal.id)
    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    _park_spend(superuser_db, task)
    superuser_db.commit()

    grant = authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=1,
        idempotency_key=f"wake-rev-{uuid.uuid4()}",
    )
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.ready

    # Re-park, revoke, reconcile must not wake.
    _park_spend(superuser_db, task)
    revoke_provider_spend(
        superuser_db, owner_id=owner.id, authorization_id=grant.id, reason="founder revoked"
    )
    superuser_db.commit()
    woken = wake_tasks_blocked_for_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
    )
    assert woken == []
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.blocked


def test_worker_reconcile_recovers_grant_without_inline_wake(superuser_db, make_verified_user, monkeypatch):
    """Simulate crash: grant exists, park remains; reconcile wakes without re-authorize."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    envelope = _envelope(superuser_db, owner.id, goal.id)
    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    _park_spend(superuser_db, task)
    superuser_db.commit()

    # Bypass inline wake to simulate lost wake after grant commit.
    monkeypatch.setattr(
        "app.mainai_execution.provider_wait_wake.wake_tasks_blocked_for_provider_spend",
        lambda *a, **k: [],
    )

    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=1,
        idempotency_key=f"wake-crash-{uuid.uuid4()}",
    )
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.blocked  # inline wake was no-op

    # Unpatch for reconcile (uses the real wake helper).
    monkeypatch.undo()
    woken = reconcile_provider_spend_parks_for_active_grants(superuser_db, limit=20)
    assert any(t.id == task.id for t in woken)
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.ready
