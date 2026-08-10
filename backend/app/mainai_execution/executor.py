"""Dispatch: a `ready` MainAITask -> a real, leased, heartbeated `mainai_jobs` row
(job_type=`task_execution`). This is the ONE place a task ever transitions out of `ready` — no
parallel queue, lease, or heartbeat mechanism is built here; the actual liveness/retry/
cancellation primitives are entirely `app/jobs/service.py`/`app/jobs/mainai_job_lease.py`'s
already-reviewed ones (see migration 0032's own docstring for why)."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.jobs import service as mainai_jobs_service
from app.mainai_execution.approval import require_task_approval
from app.models.mainai_execution import RETRYABLE_MAINAI_TASK_STATUSES, MainAIGoal, MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob


class TaskNotRetryableError(Exception):
    def __init__(self, task_id: uuid.UUID, status: MainAITaskStatus):
        super().__init__(f"MainAITask {task_id} has status '{status.value}', not retryable.")
        self.task_id = task_id


def dispatch_ready_task(db: Session, *, task: MainAITask, goal: MainAIGoal, dispatched_by: str) -> MainAIJob:
    """Creates the real mainai_jobs row that will actually execute `task`
    (app/mainai_execution/execution_job.py, dispatched from app/worker.py). Requires
    `task.status == ready` (mainai_jobs_service.create_job()'s own input_ref validation
    re-checks this against the database, not just this function's in-memory check — see
    app/jobs/service.py's _validate_task_execution_input_refs()).

    THE approval gate: require_task_approval() is called FIRST, before create_job() — an
    approval_required task with no recorded approval never gets as far as a mainai_jobs row
    existing at all. This is the literal enforcement of "no code path skips straight to
    execution" — an executor calling this function directly on an unapproved task gets
    ApprovalRequiredError, not a job."""
    if task.status != MainAITaskStatus.ready:
        raise ValueError(f"MainAITask {task.id} has status '{task.status.value}', not 'ready' -- cannot dispatch.")

    require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)

    job = mainai_jobs_service.create_job(
        db,
        owner_id=task.owner_id,
        job_type="task_execution",
        input_refs=[{"type": "mainai_task", "id": str(task.id)}],
        created_by=dispatched_by,
        idempotency_key=f"mainai_task_execution:{task.id}:{task.attempts}",
    )

    task.status = MainAITaskStatus.running
    task.started_at = task.started_at or datetime.utcnow()
    task.attempts += 1
    task.mainai_job_id = job.id
    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.dispatched, detail={"mainai_job_id": str(job.id)}))
    db.flush()

    return job


def task_for_job(db: Session, job: MainAIJob) -> MainAITask | None:
    """The inverse lookup execution_job.py needs: given a claimed `task_execution` job, find
    the MainAITask it exists to execute. `input_refs` is the job's own durable record of that
    link (see dispatch_ready_task()) -- never re-derived from `task.mainai_job_id`, which is
    the task's side of the SAME link and could theoretically be stale if a task were ever
    re-dispatched under a new job without this function's own input_refs contract (it isn't,
    in V0.1, but reading the job's own ref is the more honest source of truth for "what job
    is this run of the executor actually for")."""
    refs = [r for r in job.input_refs if r.get("type") == "mainai_task"]
    if len(refs) != 1:
        return None
    task_id = uuid.UUID(str(refs[0]["id"]))
    return db.get(MainAITask, task_id)


def retry_task(db: Session, *, task: MainAITask) -> MainAITask:
    """Moves a `retryable_failed` task back to `ready` for redispatch — the dependency check
    is deliberately NOT re-run here: the task's dependencies were already satisfied when it
    first became `ready` (recompute_task_readiness() put it there), and nothing about a
    verification failure changes that fact. Records a `retry_scheduled` event so the attempt
    count/reason is visible in the task's own history, same convention
    RETRYABLE_MAINAI_TASK_STATUSES already establishes for mainai_jobs."""
    if task.status not in RETRYABLE_MAINAI_TASK_STATUSES:
        raise TaskNotRetryableError(task.id, task.status)
    task.status = MainAITaskStatus.ready
    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.retry_scheduled, detail={"attempts": task.attempts}))
    db.flush()
    return task
