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

from sqlalchemy import func, select
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
from app.models.mainai_recovery import MainAIRecoveryRecord
from app.models.mainai_wait import MainAITaskWait

# `retryable_failed` is deliberately EXCLUDED here as of V0.3: _finalize_task_outcome()
# (execution_job.py) now ALWAYS schedules a next_retry_at for it (app/worker.py's
# `_advance_mainai_execution_retries` picks it up automatically) -- it is no longer "awaiting a
# retry decision" the way V0.1's docstring described, since that decision is now made
# unattended. A task that keeps failing eventually exhausts attempts and becomes `failed`,
# which IS still unresolved below.
_UNRESOLVED_TASK_STATUSES = frozenset({MainAITaskStatus.blocked, MainAITaskStatus.failed})


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


def _recovery_history(db: Session, task_id: uuid.UUID) -> list[dict]:
    """V0.2 integration: every dead-agent recovery attempt this task has ever gone through --
    pure aggregation over already-durable mainai_recovery_records rows, same discipline this
    module's own module docstring requires (never invents a summary, never calls an LLM). A
    task can have MORE than one recovery record across its lifetime (a takeover's own new job
    can itself later die and get recovered again), so this is always a list, oldest first --
    never collapsed into a single "was this task ever recovered" boolean, which would hide
    exactly the kind of repeated-failure pattern a founder most needs to see."""
    records = db.execute(
        select(MainAIRecoveryRecord).where(MainAIRecoveryRecord.task_id == task_id).order_by(MainAIRecoveryRecord.created_at)
    ).scalars().all()
    return [
        {
            "recovery_record_id": str(r.id),
            "dead_job_id": str(r.job_id),
            "classification": r.classification.value if r.classification is not None else None,
            "status": r.status.value,
            "salvage_action": r.salvage_action,
            "takeover_job_id": str(r.takeover_job_id) if r.takeover_job_id is not None else None,
            "manual_review_required": r.manual_review_required,
            "blocker": r.blocker,
            "detected_at": r.detected_at.isoformat(),
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in records
    ]


def _wait_history(db: Session, task_id: uuid.UUID) -> list[dict]:
    """V0.3 integration: every durable external-wait (mainai_task_waits, app/mainai_execution/
    ci_wait.py) this task has ever gone through -- same pure-aggregation discipline as
    _recovery_history() above, and for the same reason: a task can wait more than once across
    its lifetime (a takeover's new job attempt can open its own CI wait), so this is always a
    list, oldest first."""
    waits = db.execute(select(MainAITaskWait).where(MainAITaskWait.task_id == task_id).order_by(MainAITaskWait.created_at)).scalars().all()
    return [
        {
            "wait_id": str(w.id),
            "source_type": w.source_type.value,
            "status": w.status.value,
            "poll_count": w.poll_count,
            "deadline_at": w.deadline_at.isoformat(),
            "resolved_at": w.resolved_at.isoformat() if w.resolved_at else None,
            "evidence": w.evidence,
        }
        for w in waits
    ]


def _lesson_conflict_evidence(db: Session, task_id: uuid.UUID) -> list[dict]:
    """V0.3 integration: every `lesson_conflict_detected` event (app/mainai_execution/
    lesson_conflicts.py's mark_conflict()) recorded against this task -- i.e. every time a
    lesson that shaped this task's verification_plan was later found to genuinely contradict
    another lesson. Always a list, oldest first, for the same reason recovery/wait history are:
    a task can be affected by more than one disputed lesson."""
    events = (
        db.execute(
            select(MainAITaskEvent)
            .where(MainAITaskEvent.task_id == task_id, MainAITaskEvent.event_type == MainAITaskEventType.lesson_conflict_detected)
            .order_by(MainAITaskEvent.created_at)
        )
        .scalars()
        .all()
    )
    return [
        {
            "lesson_id": e.detail.get("lesson_id"),
            "conflicting_lesson_id": e.detail.get("conflicting_lesson_id"),
            "reasoning": e.detail.get("reasoning"),
            "detected_at": e.created_at.isoformat(),
        }
        for e in events
    ]


def _task_report(db: Session, task: MainAITask) -> dict:
    verification_event = _latest_event(db, task.id, {MainAITaskEventType.verification_passed, MainAITaskEventType.verification_failed})
    approval_event = _latest_event(db, task.id, {MainAITaskEventType.approval_granted})
    replan_event = _latest_event(db, task.id, {MainAITaskEventType.replanned})
    job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None
    recovery_history = _recovery_history(db, task.id)
    wait_history = _wait_history(db, task.id)
    lesson_conflicts = _lesson_conflict_evidence(db, task.id)

    # A recovery record still open (never reached `completed`) and flagged
    # manual_review_required is a REAL unresolved risk even when the task's own status still
    # reads `running` -- the task is silently stuck behind a dead job a human hasn't looked at
    # yet, which _UNRESOLVED_TASK_STATUSES alone (blocked/failed) never catches, since
    # inspect/classify() never touches MainAITask.status itself.
    unresolved_recovery = any(r["manual_review_required"] and r["status"] != "completed" for r in recovery_history)
    # V0.3: a wait that ended `failed`/`timed_out` always already drove the task through
    # _finalize_task_outcome() (execution_job.py's resume_waiting_ci_task()), so the task's own
    # status already reflects it -- this is deliberately NOT a second unresolved-risk signal, to
    # avoid double-counting the same underlying fact two different ways.
    # V0.3: a lesson this task relied on was later proven to contradict another lesson -- real,
    # actionable evidence that this task's outcome may need a founder's re-review, regardless of
    # whether the task itself already finished.
    unresolved_lesson_conflict = bool(lesson_conflicts)

    unresolved = (
        task.status in _UNRESOLVED_TASK_STATUSES
        or (task.approval_required and approval_event is None)
        or unresolved_recovery
        or unresolved_lesson_conflict
    )

    return {
        "task_id": str(task.id),
        "description": task.description,
        "task_type": task.task_type,
        "task_outcome": task.status.value,
        "liveness": task_liveness(task, job).value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "next_retry_at": task.next_retry_at.isoformat() if task.next_retry_at else None,
        "execution_attempt": (
            {
                "mainai_job_id": str(job.id),
                "status": job.status.value,
                "cancel_requested": job.cancel_requested,
                "cancel_acknowledged": job.cancel_acknowledged,
            }
            if job is not None
            else None
        ),
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
        "recovery_history": recovery_history,
        "wait_history": wait_history,
        "triggered_replan": (
            {"new_plan_version": replan_event.detail.get("new_plan_version"), "recorded_at": replan_event.created_at.isoformat()}
            if replan_event is not None
            else None
        ),
        "lesson_conflicts": lesson_conflicts,
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
    # V0.3: how many plan versions this goal has ever had -- >1 means at least one automatic
    # replan (app/mainai_execution/replan.py) happened, visible without cross-referencing every
    # task's own `triggered_replan` field.
    plan_versions_total = db.execute(select(func.count()).select_from(MainAIPlan).where(MainAIPlan.goal_id == goal_id)).scalar_one()
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
            # V0.2 integration: a goal-level rollup so a founder scanning the top of the
            # report immediately sees whether ANY task in this goal ever went through a
            # dead-agent recovery, without having to read every task's own recovery_history.
            "tasks_with_recovery_history": sum(1 for t in task_reports if t["recovery_history"]),
            "recovery_attempts_total": sum(len(t["recovery_history"]) for t in task_reports),
            # V0.3 integration: the same top-of-report rollup discipline as recovery above,
            # for the three other new long-running-orchestration signals.
            "tasks_with_wait_history": sum(1 for t in task_reports if t["wait_history"]),
            "tasks_awaiting_auto_retry": sum(1 for t in task_reports if t["next_retry_at"] is not None),
            "plan_versions_total": plan_versions_total,
            "tasks_with_disputed_lesson_evidence": sum(1 for t in task_reports if t["lesson_conflicts"]),
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


_WAITING_TASK_STATUSES = frozenset({MainAITaskStatus.waiting_ci, MainAITaskStatus.waiting_external})


def record_final_report(db: Session, *, goal: MainAIGoal) -> dict:
    """Computes the report and, ONLY if every task has reached a genuinely terminal status
    (completed/failed/cancelled -- retryable_failed is deliberately excluded: it is still
    actionable via retry_task(), so a goal with one is not yet "finished", one way or the
    other), stores it as goal.final_outcome and closes out the goal's own status/completed_at.
    A goal with any task still pending/ready/running/blocked/retryable_failed is left exactly
    as it was -- this function never forces a goal closed early.

    Also rolls up `goal.status` between `running` and `waiting`: `MainAIGoalStatus.waiting`
    (see that enum's own docstring: "a goal is waiting if ANY of its in-flight tasks is")
    existed as a schema value with no writer anywhere -- a goal with a task genuinely stuck in
    `waiting_ci`/`waiting_external` still read `running`, an honesty gap the founder's own
    review of Cursor's handoff flagged (`docs/CURSOR_ADVERSARIAL_RUNTIME_LANE_HANDOFF.md` §H.3:
    "Goal status lie"). Reuses `task_statuses`, already computed above for the terminal check,
    rather than a second query -- this IS the one place that already asks "what do this goal's
    tasks currently look like", not a parallel scan. Only ever flips `running` <-> `waiting`;
    never touches `pending`/`planning`/`blocked`/a terminal status, so it can never race or
    conflict with the terminal-close branch below or with `create_plan()`'s own
    `pending -> running` transition."""
    report = generate_goal_report(db, goal_id=goal.id)

    from app.models.mainai_execution import TERMINAL_MAINAI_TASK_STATUSES

    task_statuses = [MainAITaskStatus(t["task_outcome"]) for t in report["tasks"]]
    all_terminal = bool(task_statuses) and all(s in TERMINAL_MAINAI_TASK_STATUSES for s in task_statuses)

    if all_terminal:
        goal.final_outcome = json.dumps(report)
        goal.completed_at = datetime.utcnow()
        goal.status = MainAIGoalStatus.completed if all(s == MainAITaskStatus.completed for s in task_statuses) else MainAIGoalStatus.failed
        db.flush()
    else:
        any_waiting = any(s in _WAITING_TASK_STATUSES for s in task_statuses)
        if any_waiting and goal.status == MainAIGoalStatus.running:
            goal.status = MainAIGoalStatus.waiting
            db.flush()
        elif not any_waiting and goal.status == MainAIGoalStatus.waiting:
            goal.status = MainAIGoalStatus.running
            db.flush()

    return report
