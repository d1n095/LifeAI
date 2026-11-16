"""Life Library durable-worker package: the Postgres claim/lease primitives shared by
app/worker.py (claims jobs, owns the poll loop) and app/rag/library_import.py (renews the
lease as a heartbeat while actually doing the work) — split into their own module
specifically to avoid a circular import between those two (worker.py calls into
library_import.py's run_import_job; library_import.py needs renew_lease from here).

See app/worker.py's module docstring for why `FOR UPDATE SKIP LOCKED` plus a lease TTL is
the whole safety mechanism: no second, separate lock is needed for "only one worker owns
this job row at a time" — that's what this file's two functions together guarantee.

Pass 28 (PR #31's account-erasure slice, second founder review round) redesigned
`claim_next_job()` into a two-phase, owner-locked claim to close a real orphan-blob race a
founder review found between this function and `app/account/erasure.py`'s
`erase_account_data()`. The OLD implementation was a single, unlocked
`UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`: Postgres MVCC meant this
query's own SELECT ran against whatever snapshot was visible when it started, with NO
coordination against a concurrent erasure for the SAME owner. `erase_account_data()` acquires
`acquire_owner_erasure_lock()` (app/storage/references.py) before inventorying that owner's
`Document`/`ImportJob` storage keys — but a `pending` ImportJob a worker was ABOUT to claim
could still slip through the gap: claim's SELECT could see the row as claimable BEFORE
erasure's DELETE of it commits, letting a worker start extracting/writing NEW blobs for an
owner whose account row is about to disappear entirely underneath it — an orphaned blob with
no owner left to ever have referenced it in the deletion inventory.

The fix: never let this function touch (lock) a `knowledge_import_jobs` row before it holds
that row's owner's erasure lock — the SAME ordering `erase_account_data()` already follows
(owner-lock, then work with that owner's rows), so the two can never deadlock against each
other (whichever side acquires the owner lock first fully commits or rolls back before the
other's row-level work can even begin):
  1. **Candidate select, no lock at all** — a plain `SELECT id, owner_id ... LIMIT 1` (no
     `FOR UPDATE`) picks the oldest eligible row. This is deliberately just a hint: nothing
     stops it from picking a row a concurrent claimer or erasure is about to invalidate.
  2. **Acquire that owner's erasure lock** — `acquire_owner_erasure_lock(db, owner_id)`,
     BEFORE any row-level lock on `knowledge_import_jobs` is taken. If a concurrent
     `erase_account_data()` for the SAME owner is mid-transaction, this blocks here until it
     either commits (the candidate job, if it was this owner's, is now gone — step 3 below
     correctly finds nothing to claim) or rolls back (candidate is unaffected, step 3
     proceeds as normal). If a concurrent claim is instead racing to acquire the erasure lock
     to eventually try to claim a job for this same owner while THIS function holds it, that
     claim simply blocks in turn — symmetric, no deadlock either way.
  3. **Re-validate and claim exactly that candidate, atomically, now that its owner's lock is
     held** — an `UPDATE ... WHERE id = :candidate_id AND (still eligible) RETURNING id,
     owner_id`. Zero rows back means the candidate was claimed or deleted by someone else in
     the window between steps 1 and 2/3 (a lost race, not an error) — retry from step 1 with a
     fresh candidate. This function's own `db.commit()` after a successful claim (or after an
     exhausted candidate scan) releases the owner lock again immediately — it is held only for
     the instant of claiming, never for a job's whole processing duration.

Bounded by `max_attempts` (a safety ceiling, not a tuning knob under normal load — each lost
race means some OTHER claimer or erasure won that exact row, so a real workload converges in a
handful of iterations at most): returns None (no job claimable right now) rather than looping
forever if candidates keep churning unrealistically fast.
"""

import uuid

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.models.import_job import CLAIMABLE_IMPORT_JOB_STATUSES, ImportJobStatus
from app.storage.references import acquire_owner_erasure_lock

_IMPORT_FENCE_INFO_KEY = "import_job_lease_fence"


class ImportJobLeaseLostError(Exception):
    """The import worker no longer owns a live claim and must stop without writing state."""

    def __init__(self, job_id: uuid.UUID, worker_id: str):
        self.job_id = job_id
        self.worker_id = worker_id
        super().__init__(
            f"Import job {job_id}: live lease is no longer held by worker '{worker_id}'."
        )


_CANDIDATE_SQL = text("""
    SELECT id, owner_id FROM knowledge_import_jobs
    WHERE status = ANY(:claimable_statuses)
       OR (
            status = 'running'
            AND lease_expires_at IS NOT NULL
            AND lease_expires_at < clock_timestamp()
          )
    ORDER BY created_at ASC
    LIMIT 1
""")

_CLAIM_SQL = text("""
    WITH lease_clock AS MATERIALIZED (
        SELECT clock_timestamp() AS checked_at
    )
    UPDATE knowledge_import_jobs
    SET status = 'running',
        locked_by = :worker_id,
        lease_expires_at = lease_clock.checked_at + make_interval(secs => :lease_seconds),
        last_heartbeat_at = lease_clock.checked_at,
        started_at = COALESCE(started_at, lease_clock.checked_at)
    FROM lease_clock
    WHERE id = :candidate_id
      AND (
            status = ANY(:claimable_statuses)
            OR (
                status = 'running'
                AND lease_expires_at IS NOT NULL
                AND lease_expires_at < lease_clock.checked_at
            )
          )
    RETURNING id, owner_id
""")


def claim_next_job(
    db: Session, worker_id: str, lease_seconds: int, *, max_attempts: int = 50
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Atomically claims the oldest eligible job — a fresh `pending` row (see
    CLAIMABLE_IMPORT_JOB_STATUSES, app/models/import_job.py), OR a `running` row whose lease
    has expired (an abandoned claim from a crashed/killed worker) — and returns `(job_id,
    owner_id)`, or None if nothing is claimable right now.

    See this module's docstring for the Pass 28 two-phase, owner-locked redesign and exactly
    which orphan-blob race it closes against `app/account/erasure.py`'s
    `erase_account_data()`."""
    claimable_statuses = [s.value for s in CLAIMABLE_IMPORT_JOB_STATUSES]
    for _ in range(max_attempts):
        candidate = db.execute(_CANDIDATE_SQL, {"claimable_statuses": claimable_statuses}).first()
        if candidate is None:
            db.commit()
            return None
        candidate_id, owner_id = candidate[0], candidate[1]

        acquire_owner_erasure_lock(db, owner_id)

        row = db.execute(
            _CLAIM_SQL,
            {
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "candidate_id": str(candidate_id),
                "claimable_statuses": claimable_statuses,
            },
        ).first()
        db.commit()
        if row is not None:
            return row[0], row[1]
        # Lost the race for this exact candidate (claimed or deleted by a concurrent claimer/
        # erasure between the lock-free select above and this re-validated update) -- retry
        # with a fresh candidate rather than giving up.
    return None


def configure_import_lease_fence(
    db: Session,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
) -> None:
    """Require every commit in this session to renew the exact live import claim."""
    db.info[_IMPORT_FENCE_INFO_KEY] = {
        "job_id": str(job_id),
        "owner_id": str(owner_id),
        "worker_id": worker_id,
        "lease_seconds": lease_seconds,
    }


def clear_import_lease_fence(db: Session, job_id: uuid.UUID, worker_id: str) -> None:
    fence = db.info.get(_IMPORT_FENCE_INFO_KEY)
    if fence and fence["job_id"] == str(job_id) and fence["worker_id"] == worker_id:
        db.info.pop(_IMPORT_FENCE_INFO_KEY, None)


@event.listens_for(Session, "before_commit")
def _fence_import_commit(session: Session) -> None:
    """Fence all import-session commits, including commits inside indexing helpers.

    Lock the authoritative job row before reading the deadline, then keep the lock through
    the caller's flush and a final live-deadline check. A concurrent reclaim therefore either
    wins first (and this check fails) or waits until the authorized writes commit.
    PostgreSQL's transaction-start `now()` and a timestamp computed before a row-lock wait
    are both unsuitable for this deadline.
    """
    fence = session.info.get(_IMPORT_FENCE_INFO_KEY)
    if not fence:
        return

    connection = session.connection()
    row = connection.execute(
        text("""
            SELECT owner_id, locked_by, status, lease_expires_at
            FROM knowledge_import_jobs
            WHERE id = :job_id
            FOR UPDATE
        """),
        {"job_id": fence["job_id"]},
    ).first()
    if (
        row is None
        or str(row.owner_id) != fence["owner_id"]
        or row.locked_by != fence["worker_id"]
        or row.status != ImportJobStatus.running.value
        or row.lease_expires_at is None
    ):
        raise ImportJobLeaseLostError(uuid.UUID(fence["job_id"]), fence["worker_id"])

    checked_at = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    params = {**fence, "checked_at": checked_at}
    renewed = connection.execute(
        text("""
            UPDATE knowledge_import_jobs
            SET lease_expires_at = :checked_at + make_interval(secs => :lease_seconds),
                last_heartbeat_at = :checked_at
            WHERE id = :job_id
              AND owner_id = :owner_id
              AND locked_by = :worker_id
              AND status = 'running'
              AND lease_expires_at >= :checked_at
            RETURNING id
        """),
        params,
    ).first()
    if renewed is None:
        raise ImportJobLeaseLostError(uuid.UUID(fence["job_id"]), fence["worker_id"])

    session.flush()

    committed_at = connection.execute(text("SELECT clock_timestamp()")).scalar_one()
    renewed = connection.execute(
        text("""
            UPDATE knowledge_import_jobs
            SET lease_expires_at = :checked_at + make_interval(secs => :lease_seconds),
                last_heartbeat_at = :checked_at
            WHERE id = :job_id
              AND owner_id = :owner_id
              AND locked_by = :worker_id
              AND lease_expires_at >= :checked_at
            RETURNING id
        """),
        {**fence, "checked_at": committed_at},
    ).first()
    if renewed is None:
        raise ImportJobLeaseLostError(uuid.UUID(fence["job_id"]), fence["worker_id"])


def renew_lease(
    db: Session,
    job_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    *,
    commit: bool = True,
) -> bool:
    """The heartbeat: extends a still-in-progress job's lease so it isn't mistaken for
    abandoned and reclaimed by another worker while genuinely still being worked on. Scoped
    to the worker id, running status, and an unexpired lease. A stale worker can therefore not
    resurrect its own expired claim even before a replacement happens to reclaim it. Returns
    False without changing the row whenever any fence is stale; callers must stop immediately
    and leave recovery to the next authoritative claim."""
    result = db.execute(
        text("""
            WITH lease_clock AS MATERIALIZED (
                SELECT clock_timestamp() AS checked_at
            )
            UPDATE knowledge_import_jobs
            SET lease_expires_at = lease_clock.checked_at + make_interval(secs => :lease_seconds),
                last_heartbeat_at = lease_clock.checked_at
            FROM lease_clock
            WHERE id = :job_id
              AND locked_by = :worker_id
              AND status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at >= lease_clock.checked_at
        """),
        {"job_id": str(job_id), "worker_id": worker_id, "lease_seconds": lease_seconds},
    )
    if commit:
        db.commit()
    return result.rowcount > 0
