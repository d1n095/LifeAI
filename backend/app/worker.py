"""Life Library durable-worker package: the restart-safe worker that replaces the old
synchronous-FastAPI-BackgroundTask import path (see app/rag/library_import.py's module
docstring). Runs as its own process — a separate `worker` Docker Compose service built from
the same backend image (docker-compose.vps.yml, docker-compose.yml) — polling
`knowledge_import_jobs` directly.

Design:
  - Postgres is the source of truth for which job is claimed by which worker: claim_next_job()
    uses `SELECT ... FOR UPDATE SKIP LOCKED`, the standard safe pattern for "hand this row to
    exactly one of several competing workers" — no two worker processes (or two threads in
    the same process) can ever claim the same job at the same time, full stop, independent of
    Redis being up or configured at all.
  - The SAME query also reclaims jobs whose lease has expired (`status='running' AND
    lease_expires_at < now()`) — a worker that crashed, was OOM-killed, or lost its container
    mid-job leaves its claim behind, but only until the lease naturally expires; after that,
    ANY worker's next poll can pick the job back up and resume (app/rag/library_import.py's
    per-file checksum idempotency means resuming from the top of the file list only redoes
    the work that didn't already succeed — see run_import_job's docstring).
  - app/jobs/lock.py's Redis JobLock is layered ON TOP of the Postgres claim for a narrower
    purpose (see run_import_job's docstring) — Valkey remains coordination, not the queue
    itself; the queue is `knowledge_import_jobs`.
  - worker_concurrency (default 1, see app/config.py) bounds how many jobs this process
    handles at once — kept low by design for the current VPS's resource budget.
"""

import asyncio
import logging
import signal
import socket
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.jobs.heartbeat import record_worker_heartbeat
from app.jobs.lease import claim_next_job
from app.jobs.retry import compute_backoff_seconds, is_transient_error
from app.models.document import Document, RESUMABLE_INDEX_STATUSES
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.provider_verification import VerificationResult
from app.providers.verification import ensure_verified
from app.rag.library_import import run_import_job
from app.rag.zip_import import ZipSecurityError

logger = logging.getLogger("mainai.worker")

# knowledge_import_jobs has FORCE ROW LEVEL SECURITY (see alembic/versions/0006), scoped to
# `owner_id = current_setting('app.current_user_id')` — correct for a single request acting
# on its own user's rows, but claim_next_job() must see PENDING JOBS ACROSS ALL OWNERS before
# any owner is even known, which no per-request RLS context can satisfy. So claiming runs on
# the superuser connection (bypasses RLS unconditionally, same as tests/conftest.py's
# superuser_db fixture), while the actual per-job work after claiming uses the normal
# restricted SessionLocal — run_import_job immediately re-scopes it to that job's own owner
# via _set_rls_owner, so RLS is still fully enforced for everything the job itself touches.
_ClaimSession = sessionmaker(bind=migration_engine)


def _worker_id(settings) -> str:
    return settings.worker_id or socket.gethostname()


async def process_claimed_job(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    """Runs run_import_job to completion for one already-claimed job, retrying transient
    orchestration-level failures with STEG 11's existing backoff policy (app/jobs/retry.py) —
    a straight port of the retry loop that used to live inside library_import.py's
    run_import_job before the durable-worker package split claim/retry (worker-owned) from
    per-attempt work (library_import.py-owned). Never raises: every terminal outcome is
    written to the job row itself, exactly as before."""
    while True:
        try:
            await run_import_job(db, job_id, owner_id)
            return
        except Exception as exc:  # noqa: BLE001 - the job row is the only place this failure can safely surface
            db.rollback()
            job = db.get(ImportJob, job_id)
            if job is None:
                return
            transient = is_transient_error(exc)
            job.last_failure_transient = transient
            if transient and job.attempt_count + 1 < job.max_attempts:
                job.attempt_count += 1
                job.status = ImportJobStatus.pending
                db.add(job)
                db.commit()
                delay = compute_backoff_seconds(job.attempt_count)
                logger.warning(
                    "Import %s: tillfälligt fel (%s), försök %d/%d om %.1fs.", job_id, exc, job.attempt_count, job.max_attempts, delay
                )
                await asyncio.sleep(delay)
                continue
            job.status = ImportJobStatus.failed
            job.failure_reason = str(exc) if isinstance(exc, ZipSecurityError) else f"Oväntat fel under import: {exc}"
            job.completed_at = datetime.utcnow()
            db.add(job)
            db.commit()
            return


class Worker:
    """One worker's poll loop. `run()` is the entry point (see __main__ below); `run_once()`
    (a single claim-attempt-and-process cycle, or a no-op sleep if nothing was claimable) is
    exposed separately so tests can drive exactly one iteration deterministically instead of
    racing a real background loop."""

    def __init__(self):
        self.settings = get_settings()
        self.worker_id = _worker_id(self.settings)
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        # Graceful: only stops the loop from claiming a NEW job — a job already claimed and
        # in progress runs to its next natural stopping point (per-file boundary,
        # process_claimed_job's own return) rather than being killed mid-write. The claimed
        # job's lease still protects it even if the process is killed harder than this
        # signal allows for (see claim_next_job's reclaim-on-expired-lease behavior).
        self._shutdown.set()

    async def _requeue_blocked_jobs(self, db: Session) -> None:
        """P1: the mechanism that lets an ImportJob paused on ImportJobStatus.blocked
        (every file in it waiting on the embedding provider) resume automatically, with no
        re-upload, once a founder fixes whatever was wrong. Re-verifies the CURRENTLY ACTIVE
        embedding provider using the same cached ensure_verified() the per-file pre-flight
        check in app/rag/ingest.py uses, so this does NOT make a fresh real API call on every
        single poll cycle (default every worker_poll_interval_seconds) — only once the cache
        (PROVIDER_VERIFICATION_CACHE_SECONDS) is stale. Once it verifies ok, every `blocked`
        job is flipped back to `pending` in one cheap bulk update — no per-job loop needed,
        since they're all waiting on the exact same thing — where claim_next_job (app/jobs/
        lease.py, unchanged) picks each one up exactly like any other reclaimable job. Runs
        on the superuser claim session (same reasoning as claim_next_job itself: this must
        see and update blocked jobs across ALL owners, which no single owner's RLS context
        could satisfy)."""
        outcome = await ensure_verified(db, role="embedding")
        if outcome.result != VerificationResult.ok:
            return
        # 2026-07-28 incident: a ZIP job where SOME files genuinely failed and OTHERS paused
        # on awaiting_provider/blocked_provider rolls up to ImportJobStatus.partial, not
        # `blocked` (see library_import.py's `elif failed and (succeeded or ... or blocked or
        # ...)` branch) — the `blocked_count` column is still > 0 on that row, but the old
        # `WHERE status = 'blocked'` here never matched it, so its stuck files sat forever
        # even once the provider verified ok again. `_import_one_file`'s existing-document
        # branch already re-reports a still-failed file as `failed` again (not silently
        # reclassified as `duplicate`) on a resumed pass, so re-running a `partial` job here
        # is safe: previously-succeeded files correctly no-op as duplicates, previously-failed
        # files correctly stay counted as failed, and only the actually-blocked files are
        # really re-attempted.
        db.execute(
            text(
                "UPDATE knowledge_import_jobs SET status = 'pending' "
                "WHERE status = 'blocked' OR (status = 'partial' AND blocked_count > 0)"
            )
        )
        db.commit()

    def _reconcile_orphaned_documents(self, db: Session) -> None:
        """2026-07-28, permanent fix for a confirmed production incident:
        MAINAI_CONTEXT_BUNDLE.md's Document row was stuck at `embedding` while its ImportJob
        had already reached `completed` ("Klar") — the worker process died mid-step with no
        exception for Python to catch, so nothing ever set a terminal status on the Document,
        and once its job was terminal, no further poll cycle would ever revisit it (see
        library_import.py's `_run_once`, which now guards this same case for a job still IN
        that function — this is the equivalent guard for a job that's already terminal by the
        time the gap is noticed, e.g. one written before this fix existed).

        Finds every job already at `completed`/`partial`/`failed` that still has at least one
        linked, non-deleted Document sitting in RESUMABLE_INDEX_STATUSES, and resets ONLY that
        job's status back to `pending` (clearing completed_at/failure_reason) — never touches
        the Document row directly. claim_next_job then picks it up like any other pending job,
        and `_import_one_file`'s existing-document branch resumes the stuck Document in place
        via `_resume_incomplete_document` (same row, same durably-stored original, no
        duplicate). Idempotent and safe to run every poll cycle: a job with nothing stuck
        never matches the filter, and a job already reset to `pending` no longer matches
        either. Runs on the superuser claim session — see `_ClaimSession`'s module-level
        comment for why this must see documents/jobs across ALL owners, not just one RLS
        scope."""
        stuck_job_ids = [
            row[0]
            for row in db.query(ImportJob.id)
            .join(Document, Document.import_job_id == ImportJob.id)
            .filter(
                ImportJob.status.in_([ImportJobStatus.completed, ImportJobStatus.partial, ImportJobStatus.failed]),
                Document.deleted_at.is_(None),
                Document.status.in_(RESUMABLE_INDEX_STATUSES),
            )
            .distinct()
            .all()
        ]
        if not stuck_job_ids:
            return
        db.query(ImportJob).filter(ImportJob.id.in_(stuck_job_ids)).update(
            {"status": ImportJobStatus.pending, "completed_at": None, "failure_reason": None},
            synchronize_session=False,
        )
        db.commit()
        logger.warning(
            "Worker %s: återställde %d jobb till pending — ett kopplat dokument var fortfarande "
            "fast mitt i pipelinen trots att jobbet redan var terminalt.",
            self.worker_id,
            len(stuck_job_ids),
        )

    async def run_once(self) -> bool:
        """Returns True if a job was claimed and processed, False if there was nothing to do
        (caller should sleep before polling again). Claiming and processing deliberately use
        two different sessions/connections — see _ClaimSession's module-level comment."""
        claim_db = _ClaimSession()
        try:
            await self._requeue_blocked_jobs(claim_db)
            self._reconcile_orphaned_documents(claim_db)
            claimed = claim_next_job(claim_db, self.worker_id, self.settings.worker_lease_seconds)
        finally:
            claim_db.close()
        if claimed is None:
            return False
        job_id, owner_id = claimed
        logger.info("Worker %s: hämtade jobb %s.", self.worker_id, job_id)
        db = SessionLocal()
        try:
            await process_claimed_job(db, job_id, owner_id)
        finally:
            db.close()
        return True

    async def run(self) -> None:
        logger.info(
            "Worker %s startar (poll_interval=%.1fs, lease=%ds, concurrency=%d).",
            self.worker_id,
            self.settings.worker_poll_interval_seconds,
            self.settings.worker_lease_seconds,
            self.settings.worker_concurrency,
        )
        while not self._shutdown.is_set():
            # 2026-07-28: written on EVERY iteration, not just when a job is claimed — see
            # app/jobs/heartbeat.py's module docstring for why ImportJob.last_heartbeat_at
            # alone made an idle-but-healthy worker indistinguishable from a dead one. TTL is
            # a generous multiple of the poll interval so a couple of slow cycles never flap
            # the signal, while a genuinely crashed process still goes stale quickly.
            record_worker_heartbeat(self.worker_id, ttl_seconds=max(60, self.settings.worker_poll_interval_seconds * 5))
            try:
                worked = await self.run_once()
            except Exception:  # noqa: BLE001 - a poll-loop-level bug must never kill the whole worker process
                logger.exception("Worker %s: oväntat fel i pollningsloopen.", self.worker_id)
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=self.settings.worker_poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        logger.info("Worker %s avslutas (graciös avstängning).", self.worker_id)


def _install_signal_handlers(worker: Worker) -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_shutdown)
        except NotImplementedError:
            # Windows dev environments don't support add_signal_handler for these — the
            # worker is only ever actually deployed on Linux containers (see
            # docker-compose.vps.yml), so this is a local-dev convenience fallback only.
            signal.signal(sig, lambda *_: worker.request_shutdown())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = Worker()
    _install_signal_handlers(worker)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
