"""Claim/lease primitives for `mainai_jobs` (see migration 0025 and app/models/mainai_job.py)
— the same `SELECT ... FOR UPDATE SKIP LOCKED` + lease-TTL pattern app/jobs/lease.py already
uses for `knowledge_import_jobs`, reimplemented against this table's own columns rather than
imported, since the two tables share no columns and `claim_next_job()`'s two-phase
owner-erasure-lock coordination (see that module's docstring) does not apply here: a
`corpus_review` job (app/jobs/handlers/corpus_review.py) only ever READS existing
documents/document_chunks, it never writes a storage blob, so there is no
write-before-reference race for a concurrent account erasure to lose. A future MainAI job type
that DOES write storage would need to take `acquire_storage_key_lock`/
`acquire_owner_erasure_lock` itself, exactly like every other blob writer in this codebase
(app/storage/references.py's KNOWN_STORAGE_WRITE_PATHS) — nothing here exempts it.

Simple, single-phase claim (no owner lock) is safe here specifically because this table never
races a blob write: the only invariant that matters is "at most one worker holds this job row
at a time," which `FOR UPDATE SKIP LOCKED` alone already guarantees for the CLAIM step itself.

Lease fencing (migration 0028, founder re-review round on PR #36): claiming a job is not the
only place ownership matters — every later write against a claimed job (heartbeat/renew,
progress, proposals, terminal status) must ALSO prove it still holds the claim it thinks it
holds. `worker_id` alone cannot prove that: app/worker.py's `_worker_id()` returns a hostname-
or-configured string that can repeat across a process restart, so a worker_id-only check can't
tell a genuinely stale execution (this worker's OWN earlier claim, now reclaimed by someone
else) from a legitimately restarted one. `lease_generation` is the fencing token that closes
this: `claim_next_mainai_job()` bumps it by exactly 1 on every claim AND every reclaim, and
`renew_mainai_job_lease()` below (plus every worker-driven mutation in
app/jobs/service.py) requires the caller to present the EXACT generation it was
handed at claim time, atomically re-verified against the row's current value on every write —
not just checked once and trusted for the rest of the job's run."""

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.mainai_job import CLAIMABLE_MAINAI_JOB_STATUSES

_CLAIM_SQL = text("""
    UPDATE mainai_jobs
    SET status = 'running',
        locked_by = :worker_id,
        lease_generation = lease_generation + 1,
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        last_heartbeat_at = now(),
        started_at = COALESCE(started_at, now())
    WHERE id = (
        SELECT id FROM mainai_jobs
        WHERE status = ANY(:claimable_statuses)
           OR (
                status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < now()
                -- V0.2 (migration 0034): task_execution is the one job type with real,
                -- semi-irreversible external side effects (local git commits, GitHub pushes
                -- -- see app/mainai_execution/worktree.py) -- a dead one must never be
                -- silently handed back to whichever worker polls next. It stays `running`
                -- with an expired lease (structurally invisible to this claim, not reachable
                -- via any other path either) until
                -- app/mainai_execution/recovery_takeover.py explicitly processes it through
                -- the real inspect -> classify -> salvage gate and marks it `superseded`.
                -- Every other job type's blind reclaim-and-resume is unchanged.
                AND job_type <> 'task_execution'
           )
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, owner_id, lease_generation
""")


class JobLeaseLostError(Exception):
    """Raised by renew_mainai_job_lease() (and by every worker-driven mutation in
    app/jobs/service.py that shares this same fencing check) when the caller's
    (worker_id, lease_generation) no longer matches the job's current claim — the job was
    reclaimed by another worker (this worker's lease already expired) or is no longer
    `running` at all. The caller MUST stop all further work on this job immediately; nothing
    it does after this can be trusted to be safe, since a different claimant may already be
    processing the same job concurrently."""

    def __init__(self, job_id: uuid.UUID, worker_id: str, lease_generation: int):
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_generation = lease_generation
        super().__init__(
            f"Job {job_id}: lease no longer held by worker '{worker_id}' at generation "
            f"{lease_generation} — it was reclaimed, is no longer running, or never existed."
        )


def claim_next_mainai_job(db: Session, worker_id: str, lease_seconds: int) -> tuple[uuid.UUID, uuid.UUID, int] | None:
    """Atomically claims the oldest claimable `mainai_jobs` row (queued, or running with an
    expired lease — a crashed/killed worker's abandoned claim) and marks it `running` under
    this worker's lease, bumping `lease_generation` by 1 whether this is a fresh claim or a
    reclaim. Returns (job_id, owner_id, lease_generation), or None if nothing is claimable
    right now. Commits on success (releasing the row lock immediately, exactly like
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
    return uuid.UUID(str(row[0])), uuid.UUID(str(row[1])), int(row[2])


def renew_mainai_job_lease(db: Session, job_id: uuid.UUID, worker_id: str, lease_generation: int, lease_seconds: int) -> None:
    """Heartbeat: extends the lease and records last_heartbeat_at — called periodically by
    the job's own processing loop (app/jobs/handlers/corpus_review.py) between batches, exactly like
    ImportJob's renew_lease (app/jobs/lease.py). Does NOT commit — the caller's own batch
    transaction boundary decides when this becomes durable, so a heartbeat is never
    observable before the progress it describes actually is.

    Requires (and atomically re-verifies) the caller's own worker_id and lease_generation —
    raises JobLeaseLostError, updating NOTHING, if the row's CURRENT locked_by/lease_generation/
    status don't exactly match. This is the fix for the incident this migration/module's
    docstrings describe: before this, renew took only job_id and could not tell a stale worker
    from the real current claimant, so a worker whose lease had already been reclaimed by
    someone else could silently keep extending "its own" (no longer real) lease forever."""
    result = db.execute(
        text("""
            UPDATE mainai_jobs
            SET lease_expires_at = now() + make_interval(secs => :lease_seconds),
                last_heartbeat_at = now()
            WHERE id = :job_id AND locked_by = :worker_id AND lease_generation = :lease_generation AND status = 'running'
        """),
        {"job_id": str(job_id), "worker_id": worker_id, "lease_generation": lease_generation, "lease_seconds": lease_seconds},
    )
    if result.rowcount == 0:
        raise JobLeaseLostError(job_id, worker_id, lease_generation)


class JobNotSupersedableError(Exception):
    """Raised by mark_job_superseded() when the target job is not actually a dead, expired-
    lease `running` job -- superseding a job that is still genuinely alive (or already
    terminal) would either race a legitimate worker or silently overwrite an already-honest
    outcome. See app/mainai_execution/recovery_takeover.py, the only caller."""


def mark_job_superseded(db: Session, *, job_id: uuid.UUID, superseded_by_job_id: uuid.UUID) -> None:
    """V0.2: the honest terminal outcome for a dead `task_execution` job once a takeover has
    created its replacement (`superseded_by_job_id`). Never takes a worker_id/lease_generation
    to verify -- by construction there is no legitimate CURRENT claimant to fence against
    (task_execution jobs are structurally excluded from `claim_next_mainai_job()`'s reclaim
    branch, see that module's `_CLAIM_SQL`), so the only real safety condition is re-verified
    here atomically instead: the job must still be `running` with a lease that has genuinely
    expired. A job that is still within its lease window (a legitimately busy worker, not a
    dead one) or already terminal raises JobNotSupersedableError with NOTHING written -- this
    is the one operation in the whole recovery pipeline that is closest to being destructive
    (it retires a job row for good), so it fails closed rather than trusting the caller's own
    belief that the job is dead."""
    result = db.execute(
        text("""
            UPDATE mainai_jobs
            SET status = 'superseded',
                completed_at = now(),
                superseded_by_job_id = :superseded_by_job_id
            WHERE id = :job_id
              AND status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at < now()
        """),
        {"job_id": str(job_id), "superseded_by_job_id": str(superseded_by_job_id)},
    )
    if result.rowcount == 0:
        raise JobNotSupersedableError(f"Job {job_id} is not a dead, expired-lease running job -- refusing to supersede it.")
