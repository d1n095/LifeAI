"""First-cycle STOP-before-merge and no-paid-replan on external CI.

Local editable-task failures (repo_edit / run_tests / read_only_audit) may still trigger
automatic replan. External CI outcomes and independent-review gates must not: they STOP and
leave merge to the Founder.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.review_receipt import (
    STATE_INDEPENDENT_REVIEW_BLOCKED,
    STATE_STOP_BEFORE_MERGE,
    publication_truth,
)
from app.models.mainai_execution import (
    ACTIVE_MAINAI_GOAL_STATUSES,
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
    TERMINAL_MAINAI_GOAL_STATUSES,
)

STATE_EXTERNAL_CI_FAILED = "external_ci_failed"
STATE_EXTERNAL_CI_TIMED_OUT = "external_ci_timed_out"

LOCAL_EDITABLE_TASK_TYPES = frozenset({"repo_edit", "run_tests", "read_only_audit"})
PUBLICATION_TASK_TYPES = frozenset({"open_pr"})
STOP_STATES = frozenset(
    {
        STATE_STOP_BEFORE_MERGE,
        STATE_INDEPENDENT_REVIEW_BLOCKED,
        STATE_EXTERNAL_CI_FAILED,
        STATE_EXTERNAL_CI_TIMED_OUT,
    }
)


def is_local_editable_task(task: MainAITask) -> bool:
    return task.task_type in LOCAL_EDITABLE_TASK_TYPES


def is_publication_task(task: MainAITask) -> bool:
    return task.task_type in PUBLICATION_TASK_TYPES


def first_cycle_state_from_evidence(evidence: dict[str, Any] | None) -> str | None:
    if not isinstance(evidence, dict):
        return None
    state = evidence.get("first_cycle_state")
    return state if isinstance(state, str) and state in STOP_STATES else None


def stop_first_cycle(
    db: Session,
    task: MainAITask,
    *,
    reason: str,
    first_cycle_state: str,
    evidence: dict[str, Any],
    goal: MainAIGoal | None = None,
) -> dict[str, Any]:
    """Durable STOP: task and goal become `blocked`. Never completed, never merged, never a
    paid-replan trigger. Callers must not start a subsequent autonomous job."""
    if first_cycle_state not in STOP_STATES:
        raise ValueError(f"unknown first-cycle stop state {first_cycle_state!r}")
    truth = publication_truth(
        first_cycle_state=first_cycle_state,
        review_ok=bool(evidence.get("review_ok")),
        ci_passed=bool(evidence.get("ci_passed")),
    )
    payload = {
        **evidence,
        **truth,
        "reason": reason,
        "merged": False,
        "first_cycle_state": first_cycle_state,
    }
    task.status = MainAITaskStatus.blocked
    task.next_retry_at = None
    # blocked is not terminal — completed_at must stay NULL (ck_mainai_tasks_completed_at_matches_terminal_status).
    task.completed_at = None
    if hasattr(task, "blocker_reason"):
        task.blocker_reason = reason[:500]
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.blocked,
            detail=payload,
        )
    )
    if goal is None:
        goal = db.execute(
            select(MainAIGoal).where(MainAIGoal.id == task.goal_id, MainAIGoal.owner_id == task.owner_id)
        ).scalar_one_or_none()
    if goal is not None and goal.status in ACTIVE_MAINAI_GOAL_STATUSES and goal.status not in TERMINAL_MAINAI_GOAL_STATUSES:
        goal.status = MainAIGoalStatus.blocked
        if not goal.final_outcome:
            import json

            goal.final_outcome = json.dumps(
                {
                    "first_cycle_state": first_cycle_state,
                    "merged": False,
                    "awaiting_founder_merge": first_cycle_state == STATE_STOP_BEFORE_MERGE,
                    "reason": reason,
                    "stopped_at": datetime.utcnow().isoformat(),
                }
            )
    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)
    return payload


def should_suppress_paid_replan(task: MainAITask, *, evidence: dict[str, Any] | None = None) -> bool:
    """External CI / independent-review / STOP-before-merge must not call propose_plan_via_ai."""
    if is_publication_task(task):
        return True
    if not is_local_editable_task(task):
        return True
    return first_cycle_state_from_evidence(evidence) in STOP_STATES


def fail_closed_external_ci(
    db: Session,
    task: MainAITask,
    *,
    reason: str,
    first_cycle_state: str,
    evidence: dict[str, Any],
    goal: MainAIGoal | None = None,
) -> dict[str, Any]:
    """External CI did not pass. Permanent `failed` (no retry, no paid replan). Not merged."""
    if first_cycle_state not in {STATE_EXTERNAL_CI_FAILED, STATE_EXTERNAL_CI_TIMED_OUT}:
        raise ValueError(f"external CI stop requires an external-CI state, got {first_cycle_state!r}")
    truth = publication_truth(first_cycle_state=first_cycle_state, review_ok=False, ci_passed=False)
    payload = {**evidence, **truth, "reason": reason, "merged": False, "first_cycle_state": first_cycle_state}
    task.status = MainAITaskStatus.failed
    task.completed_at = datetime.utcnow()
    task.next_retry_at = None
    task.blocker_reason = reason[:500] if hasattr(task, "blocker_reason") else None
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.failed,
            detail=payload,
        )
    )
    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)
    return payload


def first_cycle_stop_is_active(db: Session, task: MainAITask) -> bool:
    """True when this task is in a first-cycle STOP (review/CI) that is not Founder merge."""
    if task.status not in {MainAITaskStatus.blocked, MainAITaskStatus.failed}:
        return False
    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type.in_((MainAITaskEventType.blocked, MainAITaskEventType.failed)),
        )
        .order_by(MainAITaskEvent.created_at.desc())
    ).scalars().first()
    if event is None or not isinstance(event.detail, dict):
        return False
    return first_cycle_state_from_evidence(event.detail) in STOP_STATES
