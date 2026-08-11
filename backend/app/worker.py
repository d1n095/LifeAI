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

from app.account.erasure import attempt_storage_deletion_task, claim_storage_deletion_tasks
from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.jobs.handlers.corpus_review import run_corpus_review_job
from app.jobs.handlers.message_sequence_backfill import MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE, run_message_sequence_backfill_job
from app.jobs.heartbeat import record_worker_heartbeat
from app.jobs.lease import claim_next_job
from app.jobs.mainai_job_lease import JobLeaseLostError, claim_next_mainai_job
from app.jobs.retry import compute_backoff_seconds, is_transient_error
from app.jobs.service import mark_failed, record_claimed
from app.mainai_execution.approval import ApprovalRequiredError
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.executor import dispatch_ready_task
from app.mainai_runtime_contract import CapabilityUnavailableError, require_capability
from app.models.document import Document, RESUMABLE_INDEX_STATUSES
from app.models.import_job import ImportJob, ImportJobStatus, PROVIDER_REQUEUE_STATUSES
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory
from app.models.provider_verification import VerificationResult
from app.models.storage_deletion_task import StorageDeletionTask
from app.providers.verification import ensure_verified
from app.rag.library_import import run_import_job
from app.request_context import current_user_id
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


def _set_mainai_job_rls_owner(db: Session, owner_id: uuid.UUID) -> None:
    """Same requirement and same fix as app/rag/library_import.py's `_set_rls_owner` (see
    that function's own docstring for the full reasoning): the worker's fresh session never
    goes through app/deps.py's get_current_user, and run_corpus_review_job makes several
    separate db.commit() calls as it progresses through documents, each ending the current
    transaction — so the RLS-owner contextvar must be set, not just the raw session
    variable, for every later commit's next transaction to stay correctly scoped."""
    current_user_id.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


async def process_claimed_mainai_job(
    db: Session, job_id: uuid.UUID, owner_id: uuid.UUID, worker_id: str, lease_generation: int, lease_seconds: int
) -> None:
    """Dispatches an already-claimed `mainai_jobs` row to its job_type's own processing
    function — `corpus_review` (app/jobs/handlers/corpus_review.py) and, as of S1B,
    `message_sequence_backfill` (app/jobs/handlers/message_sequence_backfill.py). Each function is
    responsible for its own terminal state transitions (completed/failed/cancelled) via
    app/jobs/service.py; this wrapper exists only to catch anything that escapes
    it unexpectedly (a bug, not a designed failure path) and still leave the job in a
    terminal, truthful state rather than stuck `running` forever with a dead claim.

    `lease_generation` is the exact fencing token claim_next_mainai_job() returned for this
    claim (migration 0028) — threaded through to every downstream write so a worker that later
    loses this lease (reclaimed by someone else) can never mutate the job again. A
    JobLeaseLostError escaping record_claimed() itself (this claim was reclaimed before the
    worker even got here — theoretically possible, never actually observed) is handled the
    same as any other lease loss: log and stop, no mark_failed attempt, since this worker no
    longer has any right to write to the row at all.

    Founder re-review round (PR #36), fourth pass: `require_capability()` is re-checked here,
    immediately before dispatch, using the exact same central policy function
    `app/jobs/service.py`'s create_job() calls at creation time — a capability that
    became unconfigured, or is now `production_prohibited`/`sandbox_only` in the CURRENT
    environment, since this job was created is caught here too, not just at creation. A job
    that fails this check is marked `failed` with `capability_unavailable`, never silently
    executed anyway."""
    _set_mainai_job_rls_owner(db, owner_id)
    job = db.get(MainAIJob, job_id, populate_existing=True)
    try:
        if job is not None:
            try:
                require_capability(db, job.job_type)
            except CapabilityUnavailableError:
                logger.warning("Worker %s: mainai_job %s's capability '%s' is no longer available at execution time.", worker_id, job_id, job.job_type)
                record_claimed(db, job, worker_id=worker_id, lease_generation=lease_generation)
                mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.capability_unavailable)
                return
        if job is not None and job.job_type == "corpus_review":
            record_claimed(db, job, worker_id=worker_id, lease_generation=lease_generation)
            await run_corpus_review_job(db, job_id, owner_id, worker_id=worker_id, lease_generation=lease_generation, lease_seconds=lease_seconds)
        elif job is not None and job.job_type == MESSAGE_SEQUENCE_BACKFILL_JOB_TYPE:
            record_claimed(db, job, worker_id=worker_id, lease_generation=lease_generation)
            await run_message_sequence_backfill_job(
                db, job_id, owner_id, worker_id=worker_id, lease_generation=lease_generation, lease_seconds=lease_seconds
            )
        elif job is not None and job.job_type == "task_execution":
            record_claimed(db, job, worker_id=worker_id, lease_generation=lease_generation)
            await run_task_execution_job(db, job_id, owner_id, worker_id=worker_id, lease_generation=lease_generation, lease_seconds=lease_seconds)
        elif job is not None:
            logger.error("Worker %s: mainai_job %s has unknown job_type '%s'.", worker_id, job_id, job.job_type)
            mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
    except JobLeaseLostError:
        logger.warning("Worker %s: lease lost for mainai_job %s before/during dispatch (generation %s).", worker_id, job_id, lease_generation)
        db.rollback()
    except Exception:  # noqa: BLE001 - the job row is the only place this failure can safely surface
        logger.exception("Worker %s: unexpected error processing mainai_job %s.", worker_id, job_id)
        db.rollback()
        job = db.get(MainAIJob, job_id)
        if job is not None:
            try:
                mark_failed(db, job, worker_id=worker_id, lease_generation=lease_generation, error_category=MainAIJobErrorCategory.unexpected)
            except JobLeaseLostError:
                logger.warning("Worker %s: lease already lost for mainai_job %s -- could not record the failure.", worker_id, job_id)


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
        #
        # 2026-07-28 correction: a `partial` job already has `completed_at` set (see
        # library_import.py's _run_once rollup) — resetting only `status` back to `pending`
        # left a row that looked simultaneously "not yet done" (status) and "already finished"
        # (completed_at), a real data-integrity inconsistency. Clearing completed_at and any
        # stale failure_reason here keeps a pending/running row honestly looking unfinished,
        # exactly like _reconcile_orphaned_documents already does for its own pending reset.
        db.execute(
            text(
                "UPDATE knowledge_import_jobs SET status = 'pending', completed_at = NULL, failure_reason = NULL "
                "WHERE status = ANY(:requeue_statuses) OR (status = 'partial' AND blocked_count > 0)"
            ),
            {"requeue_statuses": [s.value for s in PROVIDER_REQUEUE_STATUSES]},
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

    def _retry_storage_deletion_tasks(self, db: Session) -> None:
        """Pass 26 (account erasure — see app/account/erasure.py's module docstring): the
        "avgränsad maintenance-runner" that finishes any `storage_deletion_tasks` row an
        account erasure's own immediate best-effort attempt left `pending`/`failed` (a
        process crash between that erasure's DB commit and its best-effort attempt, or a
        transient storage I/O error), OR a task stuck `processing` because ITS claimer
        crashed mid-attempt (Pass 27 — see `claim_storage_deletion_tasks`'s lease-expiry
        reclaim). Deliberately a broad, CROSS-OPERATION scan — unlike
        `attempt_pending_storage_deletions_for_operation()` (account_erasure.py), which is
        scoped to the ONE operation_id a single erasure request just created — so this runs on
        the superuser claim session (`_ClaimSession`), the same reasoning `_requeue_blocked_
        jobs`/`_reconcile_orphaned_documents` above already use: this table has no owner_id
        and no RLS at all (see migration 0021's module docstring), and Pass 27 tightened
        `mainai_app`'s own grant on it down to INSERT only — this worker's superuser
        connection is now the ONLY place that ever reads or updates a `storage_deletion_tasks`
        row.

        Pass 27: claims a BOUNDED batch atomically via `claim_storage_deletion_tasks()` (the
        same `FOR UPDATE SKIP LOCKED` + lease pattern `app/jobs/lease.py`'s `claim_next_job()`
        already uses for `knowledge_import_jobs`) instead of a plain, unlocked `.all()` scan —
        so this poll cycle can never claim and re-process the same row `attempt_pending_
        storage_deletions_for_operation()`'s own immediate attempt just claimed (or another
        worker process's own poll cycle claimed), and a task whose claimer crashed becomes
        reclaimable again once its lease expires rather than sitting `processing` forever.

        Each task is individually wrapped: a failure attempting ONE task is caught, logged,
        and never stops the rest of the batch or this poll cycle — `attempt_storage_deletion_
        task` itself commits (or the caller's own except below rolls back) independently per
        task, exactly like `attempt_pending_storage_deletions_for_operation`'s own per-task
        try/except."""
        claimed_ids = claim_storage_deletion_tasks(db, limit=50)
        for task_id in claimed_ids:
            task = db.get(StorageDeletionTask, task_id)
            if task is None:
                continue
            try:
                attempt_storage_deletion_task(db, task)
            except Exception:
                db.rollback()
                logger.exception(
                    "Worker %s: återförsök av storage_deletion_task %s misslyckades.", self.worker_id, task_id
                )

    def _advance_mainai_execution_tasks(self, db: Session) -> None:
        """MainAI Execution Loop V0.1's own auto-advance tick -- the piece that makes the
        Goal/Plan/Task graph actually self-propelling instead of static. Both
        app/mainai_execution/planner.py's create_plan() and app/mainai_execution/
        execution_job.py's _finalize_task_outcome() call recompute_task_readiness()
        (app/mainai_execution/graph.py), which only ever marks a dependency-free/now-
        unblocked task `ready` in the database -- neither one dispatches it. This tick is what
        actually does that: every poll cycle, scan every `ready` MainAITask across ALL owners
        (same superuser/cross-owner reasoning as _requeue_blocked_jobs/
        _reconcile_orphaned_documents/_retry_storage_deletion_tasks above) and call
        dispatch_ready_task() for each -- the exact same approval-gated, checkpoint-aware
        dispatch path a founder-triggered API call would use, never a parallel or privileged
        shortcut.

        An approval_required task with no grant yet correctly stays `ready`
        (dispatch_ready_task() raises ApprovalRequiredError, caught here, no job created) --
        an unattended tick can never silently satisfy an approval gate, it only advances what
        is already actually clear to run. Every other exception attempting ONE task is caught,
        logged, and never stops the rest of this batch or this poll cycle, exactly like
        _retry_storage_deletion_tasks' own per-task isolation above."""
        ready_tasks = db.query(MainAITask).filter(MainAITask.status == MainAITaskStatus.ready).all()
        for task in ready_tasks:
            goal = db.get(MainAIGoal, task.goal_id)
            if goal is None:
                continue
            try:
                dispatch_ready_task(db, task=task, goal=goal, dispatched_by="mainai_worker_auto_advance")
                db.commit()
            except ApprovalRequiredError:
                db.rollback()
            except Exception:
                db.rollback()
                logger.exception("Worker %s: failed to auto-advance MainAITask %s.", self.worker_id, task.id)

    async def run_once(self) -> bool:
        """Returns True if a job was claimed and processed, False if there was nothing to do
        (caller should sleep before polling again). Claiming and processing deliberately use
        two different sessions/connections — see _ClaimSession's module-level comment.

        Tries `knowledge_import_jobs` first, then `mainai_jobs` — one shared poll loop
        servicing both queues (the founder's explicit "reuse the existing architecture"
        instruction), not a second worker process. Each poll cycle claims and fully processes
        AT MOST ONE job total across both queues, matching worker_concurrency's existing
        single-job-at-a-time budget."""
        claim_db = _ClaimSession()
        try:
            await self._requeue_blocked_jobs(claim_db)
            self._reconcile_orphaned_documents(claim_db)
            self._retry_storage_deletion_tasks(claim_db)
            self._advance_mainai_execution_tasks(claim_db)
            claimed = claim_next_job(claim_db, self.worker_id, self.settings.worker_lease_seconds)
            mainai_claimed = None if claimed is not None else claim_next_mainai_job(claim_db, self.worker_id, self.settings.worker_lease_seconds)
        finally:
            claim_db.close()

        if claimed is not None:
            job_id, owner_id = claimed
            logger.info("Worker %s: hämtade jobb %s.", self.worker_id, job_id)
            db = SessionLocal()
            try:
                await process_claimed_job(db, job_id, owner_id)
            finally:
                db.close()
            return True

        if mainai_claimed is not None:
            job_id, owner_id, lease_generation = mainai_claimed
            logger.info("Worker %s: hämtade mainai_job %s (generation %s).", self.worker_id, job_id, lease_generation)
            db = SessionLocal()
            try:
                await process_claimed_mainai_job(db, job_id, owner_id, self.worker_id, lease_generation, self.settings.worker_lease_seconds)
            finally:
                db.close()
            return True

        return False

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
