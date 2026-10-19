"""First-cycle STOP-before-merge and no-paid-replan on external CI / publication.

Local editable-task failures (repo_edit / run_tests / read_only_audit) may still trigger
automatic replan when the failure is genuinely local (verification). External CI, independent
review, GitHub push rejection, a moved approved base, and expired/denied grants must not:
they STOP the goal and leave merge to the Founder.

A first-cycle STOP is goal-scoped. Dispatch, retry, recovery, replan and scheduling treat the
goal as terminal for autonomous work even while `MainAIGoal.status` stays `blocked` (never
`completed`, never merged). Ordinary non-terminal goals are unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.publication_errors import (
    FAIL_CLOSED_ORIGINS,
    ORIGIN_TO_STOP_STATE,
    ExternalBoundaryError,
    PublicationForbiddenAfterStopError,
    STATE_EXTERNAL_BOUNDARY_FAILED as _STATE_EXTERNAL_BOUNDARY_FAILED,
)
from app.mainai_execution.review_receipt import (
    STATE_INDEPENDENT_REVIEW_BLOCKED,
    STATE_STOP_BEFORE_MERGE,
    publication_truth,
)
from app.models.mainai_execution import (
    ACTIVE_MAINAI_GOAL_STATUSES,
    TERMINAL_MAINAI_GOAL_STATUSES,
    TERMINAL_MAINAI_TASK_STATUSES,
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)

STATE_EXTERNAL_CI_FAILED = "external_ci_failed"
STATE_EXTERNAL_CI_TIMED_OUT = "external_ci_timed_out"
STATE_EXTERNAL_PUSH_REJECTED = "external_push_rejected"
STATE_APPROVED_BASE_MOVED = "approved_base_moved"
STATE_GRANT_DENIED_OR_EXPIRED = "grant_denied_or_expired"
STATE_EXTERNAL_BOUNDARY_FAILED = _STATE_EXTERNAL_BOUNDARY_FAILED

LOCAL_EDITABLE_TASK_TYPES = frozenset({"repo_edit", "run_tests", "read_only_audit"})
PUBLICATION_TASK_TYPES = frozenset({"open_pr"})
STOP_STATES = frozenset(
    {
        STATE_STOP_BEFORE_MERGE,
        STATE_INDEPENDENT_REVIEW_BLOCKED,
        STATE_EXTERNAL_CI_FAILED,
        STATE_EXTERNAL_CI_TIMED_OUT,
        STATE_EXTERNAL_PUSH_REJECTED,
        STATE_APPROVED_BASE_MOVED,
        STATE_GRANT_DENIED_OR_EXPIRED,
        STATE_EXTERNAL_BOUNDARY_FAILED,
    }
)

_PARKABLE_TASK_STATUSES = frozenset(
    {
        MainAITaskStatus.pending,
        MainAITaskStatus.ready,
        MainAITaskStatus.retryable_failed,
        MainAITaskStatus.waiting_ci,
        MainAITaskStatus.waiting_external,
        MainAITaskStatus.blocked,
    }
)


class FirstCycleGoalStoppedError(RuntimeError):
    """Autonomous dispatch/retry/recovery is forbidden because the goal is first-cycle STOP."""


def is_local_editable_task(task: MainAITask) -> bool:
    return task.task_type in LOCAL_EDITABLE_TASK_TYPES


def is_publication_task(task: MainAITask) -> bool:
    return task.task_type in PUBLICATION_TASK_TYPES


def first_cycle_state_from_evidence(evidence: dict[str, Any] | None) -> str | None:
    if not isinstance(evidence, dict):
        return None
    state = evidence.get("first_cycle_state")
    return state if isinstance(state, str) and state in STOP_STATES else None


def first_cycle_state_from_goal(goal: MainAIGoal | None) -> str | None:
    if goal is None:
        return None
    raw = goal.final_outcome
    if not raw:
        return None
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if isinstance(payload, dict):
        return first_cycle_state_from_evidence(payload)
    return None


def classify_publication_failure(
    exc: BaseException | None = None, *, evidence: dict[str, Any] | None = None
) -> str | None:
    """Return a first-cycle STOP state when the failure is an external publication cause.

    Classification is typed and origin-aware. It never reads exception messages or
    model-controlled `error` / `reason` prose. Unknown / local verification failures
    without a typed origin return None so ordinary replan stays eligible — except that
    callers at the push/grant boundary must wrap unknowns as ExternalBoundaryError
    before reaching here.
    """
    stated = first_cycle_state_from_evidence(evidence)
    if stated in STOP_STATES:
        return stated

    if isinstance(exc, ExternalBoundaryError) and exc.first_cycle_state in STOP_STATES:
        return exc.first_cycle_state

    if isinstance(evidence, dict):
        origin = evidence.get("origin") or evidence.get("remote_fetch_origin")
        if isinstance(origin, str) and origin in FAIL_CLOSED_ORIGINS:
            mapped = ORIGIN_TO_STOP_STATE.get(origin)
            if mapped in STOP_STATES:
                return mapped
        if evidence.get("remote_fetch_failed") is True:
            return STATE_EXTERNAL_BOUNDARY_FAILED
        # `failure_class` alone is model-smuggleable. Require a matching typed origin.
        # Structured flags our own code set (CI wait / review receipt), never model prose.
        if evidence.get("review_ok") is False and evidence.get("ci_passed") is True:
            return STATE_INDEPENDENT_REVIEW_BLOCKED
        if evidence.get("ci_passed") is False:
            wait = evidence.get("ci_wait") if isinstance(evidence.get("ci_wait"), dict) else {}
            if isinstance(wait, dict) and wait.get("status") == "timed_out":
                return STATE_EXTERNAL_CI_TIMED_OUT
            return STATE_EXTERNAL_CI_FAILED

    if exc is not None:
        from app.integrations.github_client import GitHubClientError
        from app.mainai_execution.worktree import (
            ActivationBaseConfigurationError,
            WorktreePushRejected,
            WorktreeTimeoutError,
        )

        if isinstance(exc, ActivationBaseConfigurationError):
            return STATE_APPROVED_BASE_MOVED
        if isinstance(exc, WorktreePushRejected):
            return STATE_EXTERNAL_PUSH_REJECTED
        if isinstance(exc, WorktreeTimeoutError):
            return STATE_EXTERNAL_BOUNDARY_FAILED
        if isinstance(exc, GitHubClientError):
            return STATE_EXTERNAL_BOUNDARY_FAILED
    return None


def latest_failure_evidence(db: Session, task: MainAITask) -> dict[str, Any] | None:
    """Newest failed / blocked / verification_failed event detail for `task`."""
    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type.in_(
                (
                    MainAITaskEventType.failed,
                    MainAITaskEventType.blocked,
                    MainAITaskEventType.verification_failed,
                )
            ),
        )
        .order_by(MainAITaskEvent.created_at.desc())
    ).scalars().first()
    if event is None or not isinstance(event.detail, dict):
        return None
    return event.detail


def goal_is_first_cycle_stopped(db: Session, goal: MainAIGoal | None) -> bool:
    """True when this goal has a durable first-cycle STOP (review / CI / publication)."""
    if goal is None:
        return False
    if first_cycle_state_from_goal(goal) in STOP_STATES:
        return True
    details = (
        db.execute(
            select(MainAITaskEvent.detail)
            .join(MainAITask, MainAITask.id == MainAITaskEvent.task_id)
            .where(
                MainAITask.goal_id == goal.id,
                MainAITaskEvent.event_type.in_(
                    (MainAITaskEventType.blocked, MainAITaskEventType.failed)
                ),
            )
            .order_by(MainAITaskEvent.created_at.desc())
            .limit(80)
        )
        .scalars()
        .all()
    )
    return any(first_cycle_state_from_evidence(detail) in STOP_STATES for detail in details)


def goal_is_autonomous_terminal(db: Session, goal: MainAIGoal | None) -> bool:
    """True when worker dispatch/retry/recovery/replan must not start more work.

    Ordinary `blocked` goals (founder input) stay schedulable. First-cycle STOP and the
    schema-terminal statuses are not.
    """
    if goal is None:
        return True
    if goal.status in TERMINAL_MAINAI_GOAL_STATUSES:
        return True
    return goal_is_first_cycle_stopped(db, goal)


def _mark_goal_stopped(
    db: Session,
    goal: MainAIGoal | None,
    *,
    first_cycle_state: str,
    reason: str,
    awaiting_founder_merge: bool,
) -> None:
    if goal is None:
        return
    if first_cycle_state_from_goal(goal) in STOP_STATES:
        return
    # Local verification can close the goal as schema-`completed` before STOP-before-merge
    # runs. First-cycle STOP is autonomously terminal but must stay `blocked`, never merged.
    if goal.status == MainAIGoalStatus.completed and first_cycle_state == STATE_STOP_BEFORE_MERGE:
        goal.status = MainAIGoalStatus.blocked
        goal.completed_at = None
    elif goal.status in TERMINAL_MAINAI_GOAL_STATUSES:
        return
    elif goal.status in ACTIVE_MAINAI_GOAL_STATUSES:
        goal.status = MainAIGoalStatus.blocked
    payload = {
        "first_cycle_state": first_cycle_state,
        "merged": False,
        "awaiting_founder_merge": awaiting_founder_merge,
        "subsequent_autonomous_job": False,
        "reason": reason,
        "stopped_at": datetime.utcnow().isoformat(),
    }
    existing: dict[str, Any] = {}
    if goal.final_outcome:
        try:
            loaded = json.loads(goal.final_outcome) if isinstance(goal.final_outcome, str) else goal.final_outcome
            if isinstance(loaded, dict):
                existing = loaded
        except json.JSONDecodeError:
            existing = {}
    # Never clobber a prior first-cycle STOP with a weaker overlay.
    if first_cycle_state_from_evidence(existing) not in STOP_STATES:
        goal.final_outcome = json.dumps({**existing, **payload})
    elif "first_cycle_state" not in existing:
        goal.final_outcome = json.dumps({**existing, **payload})


def park_remaining_goal_tasks(
    db: Session,
    *,
    goal: MainAIGoal,
    except_task_id: Any,
    reason: str,
    first_cycle_state: str,
) -> None:
    """Park every non-terminal sibling so a stopped goal cannot keep dispatching."""
    siblings = (
        db.execute(
            select(MainAITask).where(
                MainAITask.goal_id == goal.id,
                MainAITask.owner_id == goal.owner_id,
                MainAITask.id != except_task_id,
            )
        )
        .scalars()
        .all()
    )
    for sibling in siblings:
        if sibling.status in TERMINAL_MAINAI_TASK_STATUSES:
            continue
        if sibling.status == MainAITaskStatus.running:
            # Live job: request cooperative cancel in-session (request_cancel() commits and
            # cannot run inside this STOP transaction). The worker's next checkpoint and the
            # immediate pre-push recheck both refuse publication after STOP.
            sibling.next_retry_at = None
            if hasattr(sibling, "blocker_reason"):
                sibling.blocker_reason = reason[:500]
            _request_cancel_running_sibling(
                db,
                sibling,
                reason=reason,
                first_cycle_state=first_cycle_state,
            )
            continue
        if sibling.status not in _PARKABLE_TASK_STATUSES and sibling.status != MainAITaskStatus.blocked:
            sibling.next_retry_at = None
            continue
        if sibling.status == MainAITaskStatus.waiting_ci:
            from app.mainai_execution.ci_wait import cancel_ci_wait
            from app.models.mainai_wait import MainAITaskWait, MainAITaskWaitStatus

            wait = (
                db.query(MainAITaskWait)
                .filter(
                    MainAITaskWait.task_id == sibling.id,
                    MainAITaskWait.status == MainAITaskWaitStatus.pending,
                )
                .one_or_none()
            )
            if wait is not None:
                cancel_ci_wait(db, wait, reason=reason)
        was_ready = sibling.status == MainAITaskStatus.ready
        sibling.status = MainAITaskStatus.blocked
        sibling.next_retry_at = None
        sibling.completed_at = None
        if hasattr(sibling, "blocker_reason"):
            sibling.blocker_reason = reason[:500]
        db.add(
            MainAITaskEvent(
                task_id=sibling.id,
                owner_id=sibling.owner_id,
                event_type=MainAITaskEventType.blocked,
                detail={
                    "reason": reason,
                    "first_cycle_state": first_cycle_state,
                    "merged": False,
                    "parked_sibling": True,
                    "was_ready": was_ready,
                },
            )
        )


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
        "subsequent_autonomous_job": False,
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
    _mark_goal_stopped(
        db,
        goal,
        first_cycle_state=first_cycle_state,
        reason=reason,
        awaiting_founder_merge=first_cycle_state == STATE_STOP_BEFORE_MERGE,
    )
    if goal is not None:
        park_remaining_goal_tasks(
            db,
            goal=goal,
            except_task_id=task.id,
            reason=reason,
            first_cycle_state=first_cycle_state,
        )
    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)
    return payload


def _request_cancel_running_sibling(
    db: Session,
    sibling: MainAITask,
    *,
    reason: str,
    first_cycle_state: str,
) -> None:
    """Set job.cancel_requested without committing. Never call jobs.service.request_cancel here."""
    from app.models.mainai_job import (
        CANCELLABLE_MAINAI_JOB_STATUSES,
        MainAIJob,
        MainAIJobEvent,
        MainAIJobEventType,
    )

    if sibling.mainai_job_id is not None:
        job = db.get(MainAIJob, sibling.mainai_job_id)
        if (
            job is not None
            and job.status in CANCELLABLE_MAINAI_JOB_STATUSES
            and not job.cancel_requested
        ):
            job.cancel_requested = True
            db.add(
                MainAIJobEvent(
                    job_id=job.id,
                    owner_id=job.owner_id,
                    event_type=MainAIJobEventType.cancel_requested,
                    detail={
                        "reason": reason,
                        "first_cycle_state": first_cycle_state,
                        "source": "first_cycle_stop",
                    },
                )
            )
    db.add(
        MainAITaskEvent(
            task_id=sibling.id,
            owner_id=sibling.owner_id,
            event_type=MainAITaskEventType.cancel_requested,
            detail={
                "reason": reason,
                "first_cycle_state": first_cycle_state,
                "merged": False,
                "parked_running_sibling": True,
                "publication_forbidden": True,
            },
        )
    )


def assert_publication_permitted(
    db: Session,
    task: MainAITask,
    *,
    job_id: Any | None = None,
) -> None:
    """Immediate pre-push / pre-open_pr recheck.

    Locks the goal row then the task row (`SELECT … FOR UPDATE`, goal first to keep lock
    order stable). Callers must not `commit()` between this lock and the publication side
    effect. Raises PublicationForbiddenAfterStopError when the goal is already first-cycle
    STOP or schema-terminal. Founder cancel_requested is not STOP — callers re-check via
    `_check_cancel_requested`. `job_id` is accepted so publication call sites can pass the
    live job without a second signature.
    """
    del job_id

    goal = db.execute(
        select(MainAIGoal)
        .where(MainAIGoal.id == task.goal_id, MainAIGoal.owner_id == task.owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    locked_task = db.execute(
        select(MainAITask)
        .where(MainAITask.id == task.id, MainAITask.owner_id == task.owner_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        goal is None
        or locked_task is None
        or goal_is_first_cycle_stopped(db, goal)
        or goal.status in TERMINAL_MAINAI_GOAL_STATUSES
    ):
        state = first_cycle_state_from_goal(goal) if goal is not None else None
        raise PublicationForbiddenAfterStopError(
            "PublicationForbiddenAfterStop",
            first_cycle_state=state if state in STOP_STATES else STATE_STOP_BEFORE_MERGE,
        )
    # Founder cancel_requested is cooperative cancel, not first-cycle STOP. Callers re-check
    # via _check_cancel_requested immediately before push / open_pr.


def park_retryable_task_on_stopped_goal(
    db: Session, task: MainAITask, *, goal: MainAIGoal | None
) -> bool:
    """A due `retryable_failed` task on a STOP/terminal goal must not auto-retry.

    Clears `next_retry_at` and parks the task `blocked` so the worker retry tick cannot
    keep scanning it forever, and so a later accidental retry cannot resume work.
    """
    if task.status != MainAITaskStatus.retryable_failed:
        if task.next_retry_at is not None and goal_is_autonomous_terminal(db, goal):
            task.next_retry_at = None
            db.flush()
            return True
        return False
    if not goal_is_autonomous_terminal(db, goal):
        return False
    state = first_cycle_state_from_goal(goal) if goal is not None else None
    if state not in STOP_STATES:
        state = STATE_STOP_BEFORE_MERGE
    task.status = MainAITaskStatus.blocked
    task.next_retry_at = None
    task.completed_at = None
    if hasattr(task, "blocker_reason"):
        task.blocker_reason = "first-cycle STOP forbids retry"
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.blocked,
            detail={
                "reason": "first-cycle STOP forbids retry of a retryable_failed task",
                "first_cycle_state": state,
                "merged": False,
                "parked_retry_after_stop": True,
            },
        )
    )
    db.flush()
    return True


def job_has_founder_cancel(job) -> bool:
    """True when a live or dead job already carries a Founder cancel request or is cancelled."""
    if job is None:
        return False
    from app.models.mainai_job import MainAIJobStatus

    return bool(getattr(job, "cancel_requested", False)) or job.status == MainAIJobStatus.cancelled


def finalize_interrupted_job_after_founder_cancel(db: Session, *, task: MainAITask, job) -> bool:
    """Dead/interrupted job with Founder cancel_requested: cancel the task, never takeover.

    Recovery of an interrupted cancelled attempt must not mint a fresh job.
    """
    if task.status != MainAITaskStatus.running:
        return False
    if not job_has_founder_cancel(job):
        return False
    from app.jobs.mainai_job_lease import JobNotDeclinableError, mark_job_cancelled_after_dead_founder_cancel
    from app.mainai_execution.execution_job import _finalize_cancelled_task

    _finalize_cancelled_task(db, task, reason="founder_cancel_beats_recovery_takeover")
    try:
        mark_job_cancelled_after_dead_founder_cancel(db, job_id=job.id)
    except JobNotDeclinableError:
        # Job may already be terminal (`cancelled`/`failed`) while the task was stuck running.
        pass
    db.flush()
    return True


def abandon_running_task_after_stop(db: Session, task: MainAITask, *, goal: MainAIGoal | None) -> bool:
    """Park a still-running task on a STOP goal when its job is dead (no live lease).

    A still-leased running job is only cancel_requested — the worker acknowledges cooperatively.
    Never salvage, takeover, retry, or paid-replan.
    """
    from app.models.mainai_job import MainAIJob, MainAIJobStatus

    if task.status != MainAITaskStatus.running:
        return False
    if not goal_is_autonomous_terminal(db, goal):
        return False
    state = first_cycle_state_from_goal(goal) if goal is not None else None
    if state not in STOP_STATES:
        state = STATE_STOP_BEFORE_MERGE
    job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None
    if job is not None and job.status == MainAIJobStatus.running:
        _request_cancel_running_sibling(
            db,
            task,
            reason="first-cycle STOP forbids further execution of a running sibling",
            first_cycle_state=state,
        )
        # Dead lease: park immediately so recovery cannot takeover.
        if job.lease_expires_at is not None and job.lease_expires_at < datetime.utcnow():
            task.status = MainAITaskStatus.blocked
            task.next_retry_at = None
            task.completed_at = None
            if hasattr(task, "blocker_reason"):
                task.blocker_reason = "first-cycle STOP; running sibling abandoned"
            db.add(
                MainAITaskEvent(
                    task_id=task.id,
                    owner_id=task.owner_id,
                    event_type=MainAITaskEventType.blocked,
                    detail={
                        "reason": "first-cycle STOP; dead running sibling abandoned without takeover",
                        "first_cycle_state": state,
                        "merged": False,
                        "parked_running_sibling": True,
                        "abandoned_after_stop": True,
                    },
                )
            )
            return True
        return False
    task.status = MainAITaskStatus.blocked
    task.next_retry_at = None
    task.completed_at = None
    if hasattr(task, "blocker_reason"):
        task.blocker_reason = "first-cycle STOP; running sibling abandoned"
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.blocked,
            detail={
                "reason": "first-cycle STOP; running sibling with dead job abandoned without takeover",
                "first_cycle_state": state,
                "merged": False,
                "parked_running_sibling": True,
                "abandoned_after_stop": True,
            },
        )
    )
    return True


def should_suppress_paid_replan(
    task: MainAITask,
    *,
    evidence: dict[str, Any] | None = None,
    cause: BaseException | None = None,
) -> bool:
    """External CI / independent-review / publication STOP must not call propose_plan_via_ai.

    Decision is cause-based: publication-task types always suppress (open_pr never replans),
    and any task — including repo_edit — suppresses when `evidence` / `cause` classifies as an
    external publication / grant / CI / review failure. Local verification failures remain
    eligible. Error-message prose is never consulted.
    """
    if is_publication_task(task):
        return True
    if first_cycle_state_from_evidence(evidence) in STOP_STATES:
        return True
    if classify_publication_failure(cause, evidence=evidence) in STOP_STATES:
        return True
    if not is_local_editable_task(task):
        return True
    return False


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
    return fail_closed_external_publication(
        db,
        task,
        reason=reason,
        first_cycle_state=first_cycle_state,
        evidence=evidence,
        goal=goal,
    )


def fail_closed_external_publication(
    db: Session,
    task: MainAITask,
    *,
    reason: str,
    first_cycle_state: str,
    evidence: dict[str, Any],
    goal: MainAIGoal | None = None,
) -> dict[str, Any]:
    """External publication / base / grant / CI failure. Permanent failed, goal-level STOP."""
    if first_cycle_state not in STOP_STATES:
        raise ValueError(f"unknown first-cycle stop state {first_cycle_state!r}")
    review_ok = bool(evidence.get("review_ok")) if "review_ok" in evidence else False
    ci_passed = bool(evidence.get("ci_passed")) if "ci_passed" in evidence else False
    truth = publication_truth(first_cycle_state=first_cycle_state, review_ok=review_ok, ci_passed=ci_passed)
    payload = {
        **evidence,
        **truth,
        "reason": reason,
        "merged": False,
        "first_cycle_state": first_cycle_state,
        "subsequent_autonomous_job": False,
    }
    task.status = MainAITaskStatus.failed
    task.completed_at = datetime.utcnow()
    task.next_retry_at = None
    if hasattr(task, "blocker_reason"):
        task.blocker_reason = reason[:500]
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.failed,
            detail=payload,
        )
    )
    if goal is None:
        goal = db.execute(
            select(MainAIGoal).where(MainAIGoal.id == task.goal_id, MainAIGoal.owner_id == task.owner_id)
        ).scalar_one_or_none()
    _mark_goal_stopped(
        db,
        goal,
        first_cycle_state=first_cycle_state,
        reason=reason,
        awaiting_founder_merge=False,
    )
    if goal is not None:
        park_remaining_goal_tasks(
            db,
            goal=goal,
            except_task_id=task.id,
            reason=reason,
            first_cycle_state=first_cycle_state,
        )
    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)
    return payload


def first_cycle_stop_is_active(db: Session, task: MainAITask) -> bool:
    """True when this task is in a first-cycle STOP (review/CI/publication) that is not Founder merge."""
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
