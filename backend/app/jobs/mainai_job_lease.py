"""Claim/lease primitives for `mainai_jobs` (see migration 0025 and app/models/mainai_job.py)
— the same `SELECT ... FOR UPDATE SKIP LOCKED` + lease-TTL pattern app/jobs/lease.py already
uses for `knowledge_import_jobs`, reimplemented against this table's own columns rather than
imported, since the two tables share no columns and `claim_next_job()`'s two-phase
owner-erasure-lock coordination (see that module's docstring) does not apply here: a
`corpus_review` job (app/rag/corpus_review_job.py) only ever READS existing
documents/document_chunks, it never writes a storage blob, so there is no
write-before-reference race for a concurrent account erasure to lose. A future MainAI job type
that DOES write storage would need to take `acquire_storage_key_lock`/
`acquire_owner_erasure_lock` itself, exactly like every other blob writer in this codebase
(app/rag/blob_references.py's KNOWN_STORAGE_WRITE_PATHS) — nothing here exempts it.

Simple, single-phase claim (no owner lock) is safe here specifically because this table never
races a blob write: the only invariant that matters is "at most one worker holds this job row
at a time," which `FOR UPDATE SKIP LOCKED` alone already guarantees.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.mainai_job import CLAIMABLE_MAINAI_JOB_STATUSES

_CLAIM_SQL = text("""
    UPDATE mainai_jobs
    SET status = 'running',
        locked_by = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        last_heartbeat_at = now(),
        started_at = COALESCE(started_at, now())
    WHERE id = (
        SELECT id FROM mainai_jobs
        WHERE status = ANY(:claimable_statuses)
           OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < now())
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, owner_id
""")


def claim_next_mainai_job(db: Session, worker_id: str, lease_seconds: int) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Atomically claims the oldest claimable `mainai_jobs` row (queued, or running with an
    expired lease — a crashed/killed worker's abandoned claim) and marks it `running` under
    this worker's lease. Returns (job_id, owner_id), or None if nothing is claimable right
    now. Commits on success (releasing the row lock immediately, exactly like
    app/jobs/lease.py's claim_next_job) so a claim is never held open for a job's whole
    processing duration."""
    row = db.execute(
        _CLAIM_SQL,
        {
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "claimable_statuses": [s.value for s in CLAIMABLE_MAINAI_JOB_STATUSES],
        },
    ).first()
    if row is None:
        db.rollback()
        return None
    db.commit()
    return uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))


def renew_mainai_job_lease(db: Session, job_id: uuid.UUID, lease_seconds: int) -> None:
    """Heartbeat: extends the lease and records last_heartbeat_at — called periodically by
    the job's own processing loop (app/rag/corpus_review_job.py) between batches, exactly like
    ImportJob's renew_lease (app/jobs/lease.py). Does NOT commit — the caller's own batch
    transaction boundary decides when this becomes durable, so a heartbeat is never
    observable before the progress it describes actually is."""
    db.execute(
        text("""
            UPDATE mainai_jobs
            SET lease_expires_at = now() + make_interval(secs => :lease_seconds),
                last_heartbeat_at = now()
            WHERE id = :job_id
        """),
        {"job_id": str(job_id), "lease_seconds": lease_seconds},
    )
