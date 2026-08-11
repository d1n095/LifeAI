"""Dispatch: a `ready` MainAITask -> a real, leased, heartbeated `mainai_jobs` row
(job_type=`task_execution`). This is the ONE place a task ever transitions out of `ready` — no
parallel queue, lease, or heartbeat mechanism is built here; the actual liveness/retry/
cancellation primitives are entirely `app/jobs/service.py`/`app/jobs/mainai_job_lease.py`'s
already-reviewed ones (see migration 0032's own docstring for why)."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs import service as mainai_jobs_service
from app.mainai_execution.approval import require_task_approval
from app.mainai_execution.graph import recompute_task_readiness
from app.models.mainai_execution import RETRYABLE_MAINAI_TASK_STATUSES, MainAIGoal, MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob


def _lock_task(db: Session, task_id: uuid.UUID) -> MainAITask:
    """Acquires a real row-level lock (`SELECT ... FOR UPDATE`) on this task and forces the
    ORM to overwrite any already-identity-mapped copy with the CURRENT database row
    (`populate_existing=True`, the same idiom app/worker.py's `process_claimed_mainai_job`
    already uses for the identical reason) -- without this, a second call in the same Session
    for an already-loaded object would silently keep serving stale in-memory attribute values
    even though a real lock was acquired.

    Hardening pass finding (P1): dispatch_ready_task()/cancel_task()/retry_task() used to
    check `task.status` in Python and then unconditionally mutate it -- two concurrent callers
    (two auto-advance ticks from two different worker processes, or a founder double-clicking
    an API action) racing the SAME task could both pass the check and both mutate, producing a
    duplicate MainAITaskEvent (e.g. two `dispatched` events for one real dispatch) or, worse,
    a genuine lost-update between semantically incompatible outcomes (e.g. cancel vs. dispatch
    landing on the same task at once). Every task-status transition below now locks the row
    FIRST and re-checks status against the freshly locked data, closing the race at its root
    instead of patching each symptom separately."""
    return db.execute(
        select(MainAITask).where(MainAITask.id == task_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()

# A task can only be cancelled directly while nothing is actually executing it yet --
# `retryable_failed` is included since it is, functionally, "not currently running and
# waiting on a decision" exactly like `pending`/`ready`/`blocked` are. A `running` task has a
# real mainai_jobs row in flight; see cancel_task()'s own docstring for why that case is
# handled differently (and honestly documented as a real V0.1 limitation) rather than
# pretending this function can stop it.
_CANCELLABLE_MAINAI_TASK_STATUSES = frozenset(
    {MainAITaskStatus.pending, MainAITaskStatus.ready, MainAITaskStatus.blocked, MainAITaskStatus.retryable_failed}
)


class TaskNotRetryableError(Exception):
    def __init__(self, task_id: uuid.UUID, status: MainAITaskStatus):
        super().__init__(f"MainAITask {task_id} has status '{status.value}', not retryable.")
        self.task_id = task_id


class TaskNotCancellableError(Exception):
    def __init__(self, task_id: uuid.UUID, status: MainAITaskStatus):
        super().__init__(f"MainAITask {task_id} has status '{status.value}', not cancellable directly.")
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
    ApprovalRequiredError, not a job.

    Locks the task row (`_lock_task()`) BEFORE creating anything -- a concurrent second caller
    racing the same task blocks on that lock and, once it acquires it, sees the FIRST caller's
    already-committed 'running' status and cleanly raises ValueError instead of creating a
    second, orphaned mainai_jobs row nothing would ever mark as this task's own.

    Hardening pass finding (P1, found by the concurrency regression test below, not by
    inspection): the row lock alone is NOT enough. mainai_jobs_service.create_job() ends with
    a real `db.commit()` (its own SAVEPOINT-based idempotency needs a real commit to be safe —
    see its docstring) — calling it in the MIDDLE of this function, before the task's own
    status/attempts/mainai_job_id/event mutations, means create_job()'s commit releases the
    task row lock and durably creates the job BEFORE any of that task-side state exists. Two
    concurrent callers could then both pass the ready-check (the second unblocks after the
    first's commit, sees status still 'ready' because the first caller hadn't gotten around to
    writing it yet) and, worse, a crash in the gap between create_job()'s commit and this
    function's own remaining writes would leave a real, durable job with no task marked
    running, no attempts increment, and no dispatched event — a job nothing owns.

    Fixed by inverting the order: status/started_at/attempts and the dispatched event -- the
    fields that actually decide "can this task be dispatched again" and the append-only audit
    trail of what happened -- are written FIRST and flushed (not committed) while the row lock
    is still held, using a pre-generated `job_id` (mainai_jobs_service.create_job() now accepts
    one explicitly for exactly this reason). create_job() is called LAST, so its own commit is
    the ONE atomic commit for the operation's race-critical part -- the lock is held
    continuously until that single commit, and a crash before it leaves nothing durable at all
    (clean rollback on restart) rather than a half-written job.

    `task.mainai_job_id` is the one field that CANNOT be written in that same pre-commit batch:
    it is a real foreign key into mainai_jobs, so Postgres itself refuses the UPDATE until the
    referenced job row exists, which it doesn't yet at that point. It is written in a second,
    separate statement immediately after create_job() returns -- no lock needed there, because
    by then task.status is ALREADY durably 'running', which alone is enough to make this task
    unreachable by every other transition in this file (dispatch/retry/cancel all require a
    different status first). A crash in that narrow window leaves attempts/status/the
    dispatched event durable and correct, only the denormalized mainai_job_id column briefly
    unset -- recoverable, not a truthfulness gap: task_for_job() (below) deliberately resolves
    the task<->job link from the JOB's own input_refs, never from this column, for exactly this
    reason."""
    task = _lock_task(db, task.id)
    if task.status != MainAITaskStatus.ready:
        raise ValueError(f"MainAITask {task.id} has status '{task.status.value}', not 'ready' -- cannot dispatch.")

    require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)

    job_id = uuid.uuid4()
    idempotency_key = f"mainai_task_execution:{task.id}:{task.attempts}"

    task.status = MainAITaskStatus.running
    task.started_at = task.started_at or datetime.utcnow()
    task.attempts += 1
    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.dispatched, detail={"mainai_job_id": str(job_id)}))
    db.flush()

    job = mainai_jobs_service.create_job(
        db,
        owner_id=task.owner_id,
        job_type="task_execution",
        input_refs=[{"type": "mainai_task", "id": str(task.id)}],
        created_by=dispatched_by,
        idempotency_key=idempotency_key,
        job_id=job_id,
    )

    task.mainai_job_id = job.id
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
    RETRYABLE_MAINAI_TASK_STATUSES already establishes for mainai_jobs. Locks the row first
    (`_lock_task()`) so two concurrent retry calls on the same task can never both succeed and
    both record a `retry_scheduled` event."""
    task = _lock_task(db, task.id)
    if task.status not in RETRYABLE_MAINAI_TASK_STATUSES:
        raise TaskNotRetryableError(task.id, task.status)
    task.status = MainAITaskStatus.ready
    # V0.3: clear the automatic-retry-scan due-time -- it already did its job (or a founder
    # retried manually, bypassing the scan entirely); a stale value here would be inert (the
    # ready-task scan doesn't consult it) but is still worth not leaving lying around.
    task.next_retry_at = None
    db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.retry_scheduled, detail={"attempts": task.attempts}))
    db.flush()
    return task


def reset_task_for_takeover(db: Session, *, task: MainAITask) -> MainAITask:
    """V0.2: moves a task whose OWN job died back to `ready` for a takeover's fresh dispatch
    (app/mainai_execution/recovery_takeover.py). Deliberately separate from retry_task()
    (which only accepts an ALREADY-verified `retryable_failed` task, reached only through a
    real verification_failed event) -- a dead job proves nothing about whether the work would
    have passed or failed, so this must never fabricate that verdict. Only accepts a task
    genuinely still `running` (the state it's stuck at when its job died before
    _finalize_task_outcome() ever ran) -- a task in any other status was not actually
    abandoned mid-flight and has no business going through takeover at all. Locked the same
    way retry_task() is, for the same concurrent-caller reason."""
    task = _lock_task(db, task.id)
    if task.status != MainAITaskStatus.running:
        raise TaskNotRetryableError(task.id, task.status)
    task.status = MainAITaskStatus.ready
    db.add(
        MainAITaskEvent(
            task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.retry_scheduled,
            detail={"reason": "recovery_takeover", "attempts": task.attempts},
        )
    )
    db.flush()
    return task


def cancel_task(db: Session, *, task: MainAITask, cancelled_by: str, reason: str = "") -> MainAITask:
    """Cancels a task that is NOT currently running (pending/ready/blocked/retryable_failed) --
    a real, immediate, durable status transition, exactly like a replan's own supersession
    (planner.py's create_plan()) already does for a stale plan's leftover tasks.

    A `running` task is deliberately OUT OF SCOPE here, and this is an honest V0.1 limitation,
    not an oversight: it has a real mainai_jobs row already in flight (a single AI call plus a
    single verification pass, not a per-document loop like corpus_review has), with no natural
    safe mid-task checkpoint to cooperatively check a cancel flag at. Calling
    app/jobs/service.py's request_cancel() on that task's mainai_job_id still works exactly as
    it always has (it sets cancel_requested, visible in the job's own record) but
    execution_job.py does not check it mid-task -- a running task_execution job runs to its
    natural completion or failure regardless. See docs/MAINAI_EXECUTION_LOOP_V0_1.md's LIMITED
    list for this named explicitly, rather than this function silently claiming to stop
    something it cannot.

    Locks the row first (`_lock_task()`) so a cancel can never land as a lost-update against a
    concurrent dispatch/retry on the same task -- whichever caller's transaction commits first
    determines the task's real fate, and the second caller re-checks status against the
    freshly locked row instead of a possibly-stale in-memory one."""
    task = _lock_task(db, task.id)
    if task.status not in _CANCELLABLE_MAINAI_TASK_STATUSES:
        raise TaskNotCancellableError(task.id, task.status)
    task.status = MainAITaskStatus.cancelled
    task.completed_at = datetime.utcnow()
    if reason:
        task.blocker_reason = reason
    db.add(
        MainAITaskEvent(
            task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.cancelled, detail={"cancelled_by": cancelled_by, "reason": reason}
        )
    )
    db.flush()
    recompute_task_readiness(db, goal_id=task.goal_id)
    return task
