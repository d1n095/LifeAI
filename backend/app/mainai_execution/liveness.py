"""Heartbeat/liveness classification for the MainAI Execution Loop V0.1. This module computes
a read-only, point-in-time view of "is this task's execution actually alive right now" — it
never itself mutates task/job state. The underlying heartbeat mechanism is entirely the
EXISTING one (`mainai_jobs.lease_expires_at`, renewed by
app/jobs/mainai_job_lease.py's renew_mainai_job_lease(), reclaimed by claim_next_mainai_job()
— see app/mainai_execution/execution_job.py's own renew_mainai_job_lease() call). Nothing here
adds a second heartbeat/lease system; this only interprets the one that already exists.

The founder's explicit requirement this closes: `waiting_external`/`waiting_ci` must never be
misread as `stalled`, and neither `stalled` nor `dead` may ever be treated as a path to
`completed` — task_liveness() is a pure function precisely so nothing downstream can be tempted
to use its output to auto-advance a task's real status; only verify_task()'s result
(app/mainai_execution/verify.py) is ever allowed to do that."""

import enum
from datetime import datetime

from app.models.mainai_execution import TERMINAL_MAINAI_TASK_STATUSES, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus


class TaskLiveness(str, enum.Enum):
    """Not a stored column -- computed fresh from durable state every time it's asked for (see
    task_liveness()), so it can never itself drift out of sync with the task/job rows it's
    derived from."""

    idle = "idle"  # pending/ready/blocked -- no execution attempt is currently in flight
    running = "running"  # running, and the mainai_jobs lease is still within its window
    stalled = "stalled"  # running, but the lease has expired and no worker has reclaimed it yet
    dead = "dead"  # running, but the mainai_jobs row it points at is missing or already terminal -- a crash the outcome-finalize step never got to run for
    waiting_external = "waiting_external"  # never reclassified as stalled/dead regardless of lease state -- see module docstring
    waiting_ci = "waiting_ci"  # same as waiting_external
    done = "done"  # any terminal-or-retryable-exhausted status: completed/failed/cancelled/retryable_failed


# Statuses whose liveness is governed by the linked mainai_jobs row's lease, not by their own
# fixed meaning -- currently only `running`. waiting_external/waiting_ci are deliberately NOT
# here: nothing in V0.1 dispatches a task_type that ever sets those statuses (see
# docs/MAINAI_EXECUTION_LOOP_V0_1.md's LIMITED list), but the classification itself must still
# never mistake "genuinely waiting on something outside this process" for "the worker died".
_LEASE_GOVERNED_STATUSES = frozenset({MainAITaskStatus.running})


def task_liveness(task: MainAITask, job: MainAIJob | None, *, now: datetime | None = None) -> TaskLiveness:
    """Pure function: task/job state in, a liveness classification out. `now` is injectable for
    tests; defaults to the real current time."""
    now = now or datetime.utcnow()

    if task.status == MainAITaskStatus.waiting_external:
        return TaskLiveness.waiting_external
    if task.status == MainAITaskStatus.waiting_ci:
        return TaskLiveness.waiting_ci
    if task.status in TERMINAL_MAINAI_TASK_STATUSES or task.status == MainAITaskStatus.retryable_failed:
        return TaskLiveness.done
    if task.status not in _LEASE_GOVERNED_STATUSES:
        return TaskLiveness.idle  # pending/ready/blocked

    if job is None or job.status != MainAIJobStatus.running:
        # The task thinks it's running but the job it was dispatched under is gone or already
        # reached a terminal mainai_jobs state without the task's own outcome ever being
        # finalized -- exactly the crash window a checkpoint/reclaim is meant to close.
        return TaskLiveness.dead
    if job.lease_expires_at is not None and job.lease_expires_at <= now:
        return TaskLiveness.stalled
    return TaskLiveness.running
