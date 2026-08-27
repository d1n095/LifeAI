"""Adversarial invariants for canonical goal finalize via `_finalize_task_outcome`.

Proves the B5 chain is now wired into the Driver/completion gate itself:

    _finalize_task_outcome → recompute_task_readiness → record_final_report

without inventing a Supervisor-only alternate finalizer or mutating goal.status directly.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.mainai_execution import planner
from app.mainai_execution.execution_job import _finalize_task_outcome
from app.mainai_execution.final_report import record_final_report
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus


def _two_task_goal(db, owner_id):
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="finalize wire",
        original_instruction="A then B",
        created_by="test",
    )
    planner.create_plan(
        db,
        goal=goal,
        rationale="chain",
        tasks=[
            PlannedTaskSpec(description="A", task_type="read_only_audit", risk_level="low"),
            PlannedTaskSpec(
                description="B",
                task_type="read_only_audit",
                risk_level="low",
                depends_on=[0],
            ),
        ],
        created_by="test",
    )
    db.flush()
    tasks = (
        db.execute(
            select(MainAITask)
            .where(MainAITask.goal_id == goal.id)
            .order_by(MainAITask.created_at.asc(), MainAITask.id.asc())
        )
        .scalars()
        .all()
    )
    tasks[0].status = MainAITaskStatus.running
    db.flush()
    return goal, tasks[0], tasks[1]


def test_partial_graph_stays_running_after_first_finalize(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_goal(superuser_db, owner.id)
    _finalize_task_outcome(
        superuser_db, first, passed=True, evidence={"wire": "partial"}
    )
    superuser_db.commit()
    superuser_db.refresh(goal)
    superuser_db.refresh(second)
    assert first.status == MainAITaskStatus.completed
    assert second.status == MainAITaskStatus.ready
    assert goal.status == MainAIGoalStatus.running
    assert goal.final_outcome is None


def test_last_task_finalize_closes_goal_with_durable_report(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_goal(superuser_db, owner.id)
    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"wire": "a"})
    superuser_db.refresh(second)
    second.status = MainAITaskStatus.running
    superuser_db.flush()
    _finalize_task_outcome(superuser_db, second, passed=True, evidence={"wire": "b"})
    superuser_db.commit()
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert goal.completed_at is not None
    payload = json.loads(goal.final_outcome)
    assert len(payload["tasks"]) == 2


def test_record_final_report_replay_is_idempotent_after_gate_close(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_goal(superuser_db, owner.id)
    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"wire": "a"})
    superuser_db.refresh(second)
    second.status = MainAITaskStatus.running
    superuser_db.flush()
    _finalize_task_outcome(superuser_db, second, passed=True, evidence={"wire": "b"})
    superuser_db.commit()
    superuser_db.refresh(goal)
    closed_at = goal.completed_at
    outcome = goal.final_outcome
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome == outcome
    assert goal.completed_at == closed_at


def test_cancelled_goal_not_overridden_by_late_task_finalize(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_goal(superuser_db, owner.id)
    goal.status = MainAIGoalStatus.cancelled
    goal.completed_at = datetime.utcnow()
    superuser_db.flush()
    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"wire": "late"})
    # Force second terminal without going through cancel cascade.
    second.status = MainAITaskStatus.cancelled
    second.completed_at = datetime.utcnow()
    superuser_db.flush()
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.cancelled
    assert goal.final_outcome is None


def test_failed_last_task_marks_goal_failed_not_completed(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_goal(superuser_db, owner.id)
    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"wire": "a"})
    superuser_db.refresh(second)
    second.status = MainAITaskStatus.running
    second.attempts = second.max_attempts
    superuser_db.flush()
    _finalize_task_outcome(
        superuser_db, second, passed=False, evidence={"wire": "fail"}
    )
    superuser_db.commit()
    superuser_db.refresh(goal)
    assert second.status == MainAITaskStatus.failed
    assert goal.status == MainAIGoalStatus.failed
    assert goal.final_outcome is not None
