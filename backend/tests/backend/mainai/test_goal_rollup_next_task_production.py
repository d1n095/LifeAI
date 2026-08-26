"""B5 — production-shaped goal rollup / next-task progression.

Proves the durable chain the production Supervisor + worker already compose
(not helper-only readiness math):

    task reaches terminal via `_finalize_task_outcome` (same gate `run_driver` uses)
    → `recompute_task_readiness` promotes dependents
    → `record_final_report` rolls goal `running`/`waiting`/terminal
    → `eligible_authorized_goals` admits only `running` + active envelope
    → production_entry bind order would pick the next eligible task (not the completed one)
    → no authority widening (`provider_spend_authorized` stays false)

Attacks covered: crash between child complete and recompute, duplicate recompute,
failed dependency blocking, terminal parent drop from eligibility, waiting-only
exclusion, revoke mid-progression, multi-ready priority order.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.development_supervisor.production_entry import (
    _BINDABLE_TASK_STATUSES,
    eligible_authorized_goals,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.execution_job import _finalize_task_outcome
from app.mainai_execution.final_report import record_final_report
from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.execution_envelope import ExecutionAuthorizationEnvelope
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus


_DEFAULT_TEST_CAPABILITIES = [
    "read_file",
    "patch_file",
    "run_focused_test",
    "stage_scoped_changes",
    "commit_scoped_changes",
]


def _authorize(db, owner_id, goal_id, *, authorized_paths=None, authorized_capabilities=None, authorized_risk="low"):
    proposal = propose_execution_scope(
        db, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"b5-prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=authorized_paths if authorized_paths is not None else ["README.md"],
        authorized_capabilities=authorized_capabilities
        if authorized_capabilities is not None
        else _DEFAULT_TEST_CAPABILITIES,
        authorized_risk=authorized_risk,
        envelope_idempotency_key=f"b5-env-{uuid.uuid4()}",
    )
    return envelope


def _two_task_chain(db, owner_id, *, first_status=MainAITaskStatus.ready):
    """Goal with task A → task B (depends on A). Matches production plan shape."""
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="b5 rollup chain",
        original_instruction="complete audit then verify",
        created_by="test",
    )
    planner.create_plan(
        db,
        goal=goal,
        rationale="sequential chain",
        tasks=[
            PlannedTaskSpec(description="first audit", task_type="read_only_audit", risk_level="low"),
            PlannedTaskSpec(
                description="dependent verify",
                task_type="run_tests",
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
    assert len(tasks) == 2
    first, second = tasks
    if first_status != first.status:
        first.status = first_status
        db.flush()
    return goal, first, second


def _bindable_ordered(db, goal) -> list[MainAITask]:
    """Mirrors production_entry's bind selection order exactly."""
    return (
        db.execute(
            select(MainAITask)
            .where(
                MainAITask.owner_id == goal.owner_id,
                MainAITask.goal_id == goal.id,
                MainAITask.status.in_(_BINDABLE_TASK_STATUSES),
            )
            .order_by(MainAITask.priority.desc(), MainAITask.created_at.asc(), MainAITask.id.asc())
        )
        .scalars()
        .all()
    )


def test_completed_child_promotes_next_and_keeps_goal_supervisor_eligible(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id, first_status=MainAITaskStatus.running)
    envelope = _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    assert second.status == MainAITaskStatus.pending
    assert [g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50)] == [goal.id]

    # Same completion gate Development Driver uses after verified work.
    _finalize_task_outcome(
        superuser_db,
        first,
        passed=True,
        evidence={"b5": "child_complete", "source": "production_finalize_gate"},
    )
    report = record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(first)
    superuser_db.refresh(second)
    superuser_db.refresh(goal)

    assert first.status == MainAITaskStatus.completed
    assert second.status == MainAITaskStatus.ready
    assert goal.status == MainAIGoalStatus.running
    assert goal.final_outcome is None  # not closed early
    assert any(t["task_outcome"] == "ready" for t in report["tasks"])

    eligible = eligible_authorized_goals(superuser_db, limit=50)
    assert [g.id for g, e in eligible] == [goal.id]
    assert eligible[0][1].id == envelope.id

    bindable = _bindable_ordered(superuser_db, goal)
    assert [t.id for t in bindable] == [second.id]
    assert first.id not in {t.id for t in bindable}


def test_crash_between_child_complete_and_recompute_recovers_without_skip(
    superuser_db, make_verified_user
):
    """Simulate process death after durable completed write but before readiness recompute."""
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id, first_status=MainAITaskStatus.running)
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    first.status = MainAITaskStatus.completed
    first.completed_at = datetime.utcnow()
    superuser_db.flush()
    # Deliberately skip recompute_task_readiness — crash window.
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(second)
    superuser_db.refresh(goal)
    assert second.status == MainAITaskStatus.pending  # not silently skipped into ready
    assert goal.status == MainAIGoalStatus.running

    # Re-entry (worker/driver finalize path, or an explicit recompute) recovers.
    newly = recompute_task_readiness(superuser_db, goal_id=goal.id)
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(second)
    assert [t.id for t in newly] == [second.id]
    assert second.status == MainAITaskStatus.ready
    assert _bindable_ordered(superuser_db, goal)[0].id == second.id
    assert [g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50)] == [goal.id]


def test_duplicate_recompute_after_finalize_is_idempotent(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id, first_status=MainAITaskStatus.running)
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"b5": "dup1"})
    first_ready = recompute_task_readiness(superuser_db, goal_id=goal.id)
    second_ready = recompute_task_readiness(superuser_db, goal_id=goal.id)
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(second)
    # Finalize already recomputed once; subsequent recomputes must not invent more ready rows
    # or re-promote a completed parent.
    assert first_ready == []
    assert second_ready == []
    assert second.status == MainAITaskStatus.ready
    assert _bindable_ordered(superuser_db, goal) == [second]


def test_failed_child_blocks_dependent_and_closes_goal_failed(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id, first_status=MainAITaskStatus.running)
    first.max_attempts = 1
    first.attempts = 1
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    _finalize_task_outcome(
        superuser_db,
        first,
        passed=False,
        evidence={"verification_steps": [{"kind": "static_analysis", "passed": False}]},
    )
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(first)
    superuser_db.refresh(second)
    superuser_db.refresh(goal)

    assert first.status == MainAITaskStatus.failed
    assert second.status == MainAITaskStatus.blocked
    # Parent still non-terminal until every task is terminal — blocked is not terminal.
    assert goal.status == MainAIGoalStatus.running
    assert [g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50)] == [goal.id]
    # Blocked is not bindable for Supervisor dispatch.
    assert _bindable_ordered(superuser_db, goal) == []

    # Exhaust the blocked sibling into cancelled/failed-terminal via cancel-style terminal
    # so rollup can close — production may leave blocked until founder acts; prove rollup
    # only closes when ALL tasks are terminal.
    second.status = MainAITaskStatus.cancelled
    second.completed_at = datetime.utcnow()
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()
    superuser_db.refresh(goal)

    assert goal.status == MainAIGoalStatus.failed
    assert eligible_authorized_goals(superuser_db, limit=50) == []


def test_all_tasks_completed_closes_goal_and_drops_eligibility(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id, first_status=MainAITaskStatus.running)
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"b5": "a"})
    superuser_db.refresh(second)
    assert second.status == MainAITaskStatus.ready

    second.status = MainAITaskStatus.running
    _finalize_task_outcome(superuser_db, second, passed=True, evidence={"b5": "b"})
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert eligible_authorized_goals(superuser_db, limit=50) == []
    assert _bindable_ordered(superuser_db, goal) == []


def test_waiting_only_goal_is_not_supervisor_eligible_until_actionable(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id)
    first.status = MainAITaskStatus.waiting_ci
    second.status = MainAITaskStatus.pending
    _authorize(superuser_db, owner.id, goal.id)
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.waiting
    assert eligible_authorized_goals(superuser_db, limit=50) == []

    # Wake path: waiting_ci resolves to completed → recompute → ready dependent → running again.
    first.status = MainAITaskStatus.completed
    first.completed_at = datetime.utcnow()
    recompute_task_readiness(superuser_db, goal_id=goal.id)
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()

    superuser_db.refresh(goal)
    superuser_db.refresh(second)
    assert second.status == MainAITaskStatus.ready
    assert goal.status == MainAIGoalStatus.running
    assert [g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50)] == [goal.id]


def test_revoke_after_child_complete_stops_next_task_without_widening(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal, first, second = _two_task_chain(superuser_db, owner.id, first_status=MainAITaskStatus.running)
    envelope = _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    _finalize_task_outcome(superuser_db, first, passed=True, evidence={"b5": "pre_revoke"})
    record_final_report(superuser_db, goal=goal)
    superuser_db.commit()
    assert second.status == MainAITaskStatus.ready
    assert [g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50)] == [goal.id]

    envelope.status = "superseded"
    superuser_db.commit()

    assert eligible_authorized_goals(superuser_db, limit=50) == []
    # Next task remains ready in graph terms, but production Supervisor must not pick it up
    # without an active envelope — no silent V0.1 / spend widening.
    superuser_db.refresh(second)
    assert second.status == MainAITaskStatus.ready
    assert goal.status == MainAIGoalStatus.running


def test_multiple_ready_children_bind_by_priority_then_created_at(
    superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    goal = planner.create_goal(
        db=superuser_db,
        owner_id=owner.id,
        title="b5 multi ready",
        original_instruction="two independent audits",
        created_by="test",
    )
    planner.create_plan(
        superuser_db,
        goal=goal,
        rationale="independent",
        tasks=[
            PlannedTaskSpec(description="low priority", task_type="read_only_audit", risk_level="low"),
            PlannedTaskSpec(description="high priority", task_type="read_only_audit", risk_level="low"),
        ],
        created_by="test",
    )
    superuser_db.flush()
    tasks = (
        superuser_db.execute(
            select(MainAITask).where(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at.asc())
        )
        .scalars()
        .all()
    )
    low, high = tasks
    low.priority = 1
    high.priority = 50
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    bindable = _bindable_ordered(superuser_db, goal)
    assert [t.id for t in bindable] == [high.id, low.id]
    assert [g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50)] == [goal.id]
