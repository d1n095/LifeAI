"""Durable final report generation for the MainAI Execution Loop V0.1. generate_goal_report()
is a pure aggregation over already-durable rows (mainai_goals/mainai_plans/mainai_tasks/
mainai_task_events) — it never calls an LLM and never invents a summary sentence; every field
in its output traces back to a specific column or event this codebase already wrote for a
different reason (state transitions, verification evidence, approval grants). This is the
founder's explicit requirement: a report generated from durable state, not free model text.

The report keeps four things that are semantically DIFFERENT deliberately separate, never
collapsed into one status field, because collapsing them is exactly the mistake the founder
flagged after the executor checkpoint: a mainai_jobs row reaching `completed` means the
execution ATTEMPT finished running -- it does not mean the task's actual goal was achieved.
  - execution attempt status  (the task's most recent mainai_jobs row, if any)
  - task outcome              (MainAITask.status itself)
  - verification outcome      (the task's latest verification_passed/verification_failed event)
  - approval state            (whether approval was required, and whether it was granted)
`unresolved_risk` flags a task that still needs founder attention: blocked, retryable_failed
awaiting a retry decision, or approval_required with no grant recorded yet."""

import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.liveness import task_liveness
from app.models.mainai_execution import (
    MainAIGoal,
    MainAIGoalStatus,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob

_UNRESOLVED_TASK_STATUSES = frozenset({MainAITaskStatus.blocked, MainAITaskStatus.retryable_failed, MainAITaskStatus.failed})


class GoalNotFoundError(Exception):
    def __init__(self, goal_id: uuid.UUID):
        super().__init__(f"MainAIGoal {goal_id} not found.")


def _latest_event(db: Session, task_id: uuid.UUID, event_types: set[MainAITaskEventType]) -> MainAITaskEvent | None:
    return db.execute(
        select(MainAITaskEvent)
        .where(MainAITaskEvent.task_id == task_id, MainAITaskEvent.event_type.in_(event_types))
        .order_by(MainAITaskEvent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _task_report(db: Session, task: MainAITask) -> dict:
    verification_event = _latest_event(db, task.id, {MainAITaskEventType.verification_passed, MainAITaskEventType.verification_failed})
    approval_event = _latest_event(db, task.id, {MainAITaskEventType.approval_granted})
    job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None

    unresolved = task.status in _UNRESOLVED_TASK_STATUSES or (task.approval_required and approval_event is None)

    return {
        "task_id": str(task.id),
        "description": task.description,
        "task_type": task.task_type,
        "task_outcome": task.status.value,
        "liveness": task_liveness(task, job).value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "execution_attempt": {"mainai_job_id": str(job.id), "status": job.status.value} if job is not None else None,
        "verification_outcome": (
            {"passed": verification_event.event_type == MainAITaskEventType.verification_passed, "recorded_at": verification_event.created_at.isoformat()}
            if verification_event is not None
            else None
        ),
        "approval": {
            "required": task.approval_required,
            "granted": approval_event is not None,
            "granted_by": approval_event.detail.get("approved_by") if approval_event is not None else None,
        },
        "unresolved_risk": unresolved,
        "blocker_reason": task.blocker_reason,
    }


def generate_goal_report(db: Session, *, goal_id: uuid.UUID) -> dict:
    """Assembles the full report for `goal_id` from durable state only. Safe to call at any
    point in a goal's lifecycle, not just once it's finished -- an in-progress goal simply
    shows in-flight tasks with `execution_attempt.status == "running"` and no verification
    outcome yet, rather than refusing to report anything."""
    goal = db.get(MainAIGoal, goal_id)
    if goal is None:
        raise GoalNotFoundError(goal_id)

    plan = db.execute(select(MainAIPlan).where(MainAIPlan.goal_id == goal_id, MainAIPlan.status == MainAIPlanStatus.active)).scalar_one_or_none()
    tasks = db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id).order_by(MainAITask.created_at)).scalars().all()
    task_reports = [_task_report(db, task) for task in tasks]

    counts: dict[str, int] = {}
    for t in task_reports:
        counts[t["task_outcome"]] = counts.get(t["task_outcome"], 0) + 1

    return {
        "goal": {
            "id": str(goal.id),
            "title": goal.title,
            "status": goal.status.value,
            "risk_level": goal.risk_level.value,
            "approval_policy": goal.approval_policy,
            "started_at": goal.started_at.isoformat() if goal.started_at else None,
            "completed_at": goal.completed_at.isoformat() if goal.completed_at else None,
        },
        "plan": {"version": plan.version, "status": plan.status.value, "rationale": plan.rationale} if plan is not None else None,
        "tasks": task_reports,
        "summary": {
            "total_tasks": len(task_reports),
            "by_outcome": counts,
            "unresolved_risk_count": sum(1 for t in task_reports if t["unresolved_risk"]),
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


def record_final_report(db: Session, *, goal: MainAIGoal) -> dict:
    """Computes the report and, ONLY if every task has reached a genuinely terminal status
    (completed/failed/cancelled -- retryable_failed is deliberately excluded: it is still
    actionable via retry_task(), so a goal with one is not yet "finished", one way or the
    other), stores it as goal.final_outcome and closes out the goal's own status/completed_at.
    A goal with any task still pending/ready/running/blocked/retryable_failed is left exactly
    as it was -- this function never forces a goal closed early."""
    report = generate_goal_report(db, goal_id=goal.id)

    from app.models.mainai_execution import TERMINAL_MAINAI_TASK_STATUSES

    task_statuses = [MainAITaskStatus(t["task_outcome"]) for t in report["tasks"]]
    all_terminal = bool(task_statuses) and all(s in TERMINAL_MAINAI_TASK_STATUSES for s in task_statuses)

    if all_terminal:
        goal.final_outcome = json.dumps(report)
        goal.completed_at = datetime.utcnow()
        goal.status = MainAIGoalStatus.completed if all(s == MainAITaskStatus.completed for s in task_statuses) else MainAIGoalStatus.failed
        db.flush()

    return report
