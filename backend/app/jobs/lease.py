"""Life Library durable-worker package: the Postgres claim/lease primitives shared by
app/worker.py (claims jobs, owns the poll loop) and app/rag/library_import.py (renews the
lease as a heartbeat while actually doing the work) — split into their own module
specifically to avoid a circular import between those two (worker.py calls into
library_import.py's run_import_job; library_import.py needs renew_lease from here).

See app/worker.py's module docstring for why `FOR UPDATE SKIP LOCKED` plus a lease TTL is
the whole safety mechanism: no second, separate lock is needed for "only one worker owns
this job row at a time" — that's what this file's two functions together guarantee.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.import_job import CLAIMABLE_IMPORT_JOB_STATUSES


def claim_next_job(db: Session, worker_id: str, lease_seconds: int) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Atomically claims the oldest eligible job — a fresh `pending` row (see
    CLAIMABLE_IMPORT_JOB_STATUSES, app/models/import_job.py), OR a `running` row whose lease
    has expired (an abandoned claim from a crashed/killed worker) — and returns `(job_id,
    owner_id)`, or None if nothing is claimable right now."""
    row = db.execute(
        text("""
            UPDATE knowledge_import_jobs
            SET status = 'running',
                locked_by = :worker_id,
                lease_expires_at = now() + make_interval(secs => :lease_seconds),
                last_heartbeat_at = now(),
                started_at = COALESCE(started_at, now())
            WHERE id = (
                SELECT id FROM knowledge_import_jobs
                WHERE status = ANY(:claimable_statuses)
                   OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < now())
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, owner_id
        """),
        {
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "claimable_statuses": [s.value for s in CLAIMABLE_IMPORT_JOB_STATUSES],
        },
    ).first()
    db.commit()
    if row is None:
        return None
    return row[0], row[1]


def renew_lease(db: Session, job_id: uuid.UUID, worker_id: str, lease_seconds: int) -> bool:
    """The heartbeat: extends a still-in-progress job's lease so it isn't mistaken for
    abandoned and reclaimed by another worker while genuinely still being worked on. Scoped
    to `locked_by = :worker_id` so a worker whose lease already expired and was reclaimed by
    someone else can't accidentally renew a claim it no longer actually holds — returns False
    in that case (0 rows matched), which callers treat as "stop, someone else owns this job
    now" rather than a fatal error (see app/rag/library_import.py's per-file heartbeat)."""
    result = db.execute(
        text("""
            UPDATE knowledge_import_jobs
            SET lease_expires_at = now() + make_interval(secs => :lease_seconds),
                last_heartbeat_at = now()
            WHERE id = :job_id AND locked_by = :worker_id
        """),
        {"job_id": str(job_id), "worker_id": worker_id, "lease_seconds": lease_seconds},
    )
    db.commit()
    return result.rowcount > 0
