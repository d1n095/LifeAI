"""Domain service for `mainai_jobs` (see migration 0025, app/models/mainai_job.py,
app/mainai_runtime_contract.py). Every mutation here also writes an append-only
MainAIJobEvent and an AuditLog entry (create/start/cancel/retry/complete/fail — the founder's
explicit requirement) — never just updates the row silently.

Router functions (app/routers/mainai_jobs.py) call these against the ordinary, RLS-scoped
`SessionLocal` — owner isolation is enforced by Postgres itself (see app/rls.py), not
re-implemented here. The one exception is the worker's claim step
(app/jobs/mainai_job_lease.py), which necessarily runs on the superuser/migration connection
since it must see jobs across every owner before any single owner's RLS context could apply —
exactly the same split app/worker.py already uses for `knowledge_import_jobs`.
"""

import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.jobs.mainai_job_lease import JobLeaseLostError
from app.mainai_runtime_contract import CapabilityUnavailableError, require_capability
from app.models.document import Document, IndexStatus
from app.models.mainai_job import (
    CANCELLABLE_MAINAI_JOB_STATUSES,
    RETRYABLE_MAINAI_JOB_STATUSES,
    MainAIJob,
    MainAIJobErrorCategory,
    MainAIJobEvent,
    MainAIJobEventType,
    MainAIJobStatus,
)

logger = logging.getLogger("mainai.jobs")

# Fixed, reviewed mapping from the closed error_category vocabulary to what a caller actually
# sees — MainAIJob.public_message is set from this table, NEVER from str(exception). Adding a
# new MainAIJobErrorCategory member without adding an entry here is a KeyError at the call
# site, not a silent leak of raw error text — see mark_failed() below.
_PUBLIC_ERROR_MESSAGES: dict[MainAIJobErrorCategory, str] = {
    MainAIJobErrorCategory.transient_io: "A temporary error occurred. This job will not retry automatically — use retry.",
    MainAIJobErrorCategory.permanent: "This job could not complete due to a permanent error and cannot be retried.",
    MainAIJobErrorCategory.capability_unavailable: "The requested capability is not currently available.",
    MainAIJobErrorCategory.cancelled_by_owner: "This job was cancelled.",
    MainAIJobErrorCategory.timeout: "This job did not complete in time.",
    MainAIJobErrorCategory.unexpected: "An unexpected error occurred while processing this job.",
}


class JobNotFoundError(Exception):
    pass


class InvalidJobTransitionError(Exception):
    """Raised for a cancel/retry request that does not apply to the job's current status —
    e.g. retrying a job that already succeeded. Mapped to HTTP 409 by the router."""


class InvalidInputRefsError(Exception):
    """Raised when a create_job caller references a document that does not exist, is not
    owned by the caller, or is not yet indexed — validated eagerly, before any job row is
    created, so a job can never be left claiming to process material it was never actually
    entitled to touch."""


class IdempotencyConflictError(Exception):
    """Founder re-review round (PR #36), fourth pass: raised when `(owner_id, idempotency_key)`
    matches an EXISTING job, but the new call's `job_type`/`input_refs` differ from that job's
    own — mapped to HTTP 409 by the router. Replaces the previous, undocumented-and-untested
    policy of silently returning the original job regardless of payload: the founder's explicit
    choice is that a genuinely different request under a reused key is a conflict, not a
    silent no-op. A caller that wants a new job must use a new idempotency_key; a caller
    replaying its own exact original request (same job_type, same input_refs) still gets the
    original job back, unchanged, as before."""


def _canonical_request_fingerprint(job_type: str, input_refs: list[dict]) -> str:
    """A stable, order-independent representation of "what job was actually requested" —
    compared between a new create_job() call and an existing job under the same idempotency
    key to tell a genuine replay (identical fingerprint) apart from a reused key with a
    materially different request (different fingerprint -> IdempotencyConflictError). Only
    `type`/`id` are compared per ref (the two fields _validate_input_refs actually requires) so
    an extra, ignored key some future caller adds to a ref dict doesn't spuriously change the
    fingerprint."""
    normalized_refs = sorted(json.dumps({"type": r.get("type"), "id": r.get("id")}, sort_keys=True) for r in input_refs)
    return json.dumps({"job_type": job_type, "input_refs": normalized_refs}, sort_keys=True)


def _record_event(db: Session, job: MainAIJob, event_type: MainAIJobEventType, detail: dict | None = None) -> MainAIJobEvent:
    event = MainAIJobEvent(job_id=job.id, owner_id=job.owner_id, event_type=event_type, detail=detail or {})
    db.add(event)
    return event


def _guarded_job_write(db: Session, job_id: uuid.UUID, *, worker_id: str, lease_generation: int, set_sql: str, params: dict) -> None:
    """The ONLY way any worker-driven code may mutate a claimed `mainai_jobs` row (migration
    0028, founder re-review round on PR #36 — see app/jobs/mainai_job_lease.py's module
    docstring for the incident this closes). Atomically re-verifies, in the SAME statement as
    the write itself, that `worker_id`/`lease_generation` still exactly match the row's
    CURRENT claim and that it is still `running` — never trusts an in-memory ORM `job` object,
    which may already be stale by the time this runs. Raises JobLeaseLostError (updating
    NOTHING) the instant that's no longer true, whether because another worker legitimately
    reclaimed this job (this worker's own lease expired) or because the job already reached a
    terminal state through some other path.

    A zero-rowcount UPDATE is indistinguishable, by design, between "the row doesn't exist"
    and "the row exists but the WHERE clause didn't match" — both are correctly treated as
    lease-lost here, since neither case gives this caller any right to keep acting on the job."""
    result = db.execute(
        text(f"UPDATE mainai_jobs SET {set_sql} WHERE id = :job_id AND locked_by = :worker_id "
             "AND lease_generation = :lease_generation AND status = 'running'"),
        {**params, "job_id": str(job_id), "worker_id": worker_id, "lease_generation": lease_generation},
    )
    if result.rowcount == 0:
        raise JobLeaseLostError(job_id, worker_id, lease_generation)


def _validate_input_refs(db: Session, owner_id: uuid.UUID, input_refs: list[dict]) -> None:
    """corpus_review's own contract: every ref must be `{"type": "document", "id": "..."}`
    pointing at a document this owner's own RLS scope can see (enforced by the query itself,
    not a manual owner_id check) that has actually finished indexing. A ref to a document
    still mid-pipeline, failed, or soft-deleted is rejected up front rather than silently
    skipped later."""
    if not input_refs:
        raise InvalidInputRefsError("input_refs must reference at least one existing document.")
    for ref in input_refs:
        if not isinstance(ref, dict) or ref.get("type") != "document" or not ref.get("id"):
            raise InvalidInputRefsError(f"Invalid input_ref (expected {{'type': 'document', 'id': ...}}): {ref!r}")
        try:
            doc_id = uuid.UUID(str(ref["id"]))
        except ValueError as exc:
            raise InvalidInputRefsError(f"Invalid document id in input_refs: {ref['id']!r}") from exc
        doc = db.execute(
            select(Document).where(Document.id == doc_id, Document.uploaded_by == owner_id, Document.deleted_at.is_(None))
        ).scalar_one_or_none()
        if doc is None:
            raise InvalidInputRefsError(f"Document {doc_id} not found or not owned by this account.")
        if doc.status != IndexStatus.indexed:
            raise InvalidInputRefsError(f"Document {doc_id} has status '{doc.status.value}', not 'indexed' — not yet reviewable.")


def _validate_task_execution_input_refs(db: Session, owner_id: uuid.UUID, input_refs: list[dict]) -> None:
    """task_execution's own contract (MainAI Execution Loop V0.1,
    app/mainai_execution/executor.py): exactly one `{"type": "mainai_task", "id": "..."}` ref,
    pointing at a real MainAITask this owner's own RLS scope can see, that is currently
    `ready` — a task in any other status (already running, blocked, completed, ...) is not a
    valid dispatch target."""
    from app.models.mainai_execution import MainAITask, MainAITaskStatus  # local import: avoids a hard app.jobs -> app.mainai_execution dependency for callers that never touch task_execution

    if len(input_refs) != 1:
        raise InvalidInputRefsError(f"task_execution requires exactly one input_ref, got {len(input_refs)}.")
    ref = input_refs[0]
    if not isinstance(ref, dict) or ref.get("type") != "mainai_task" or not ref.get("id"):
        raise InvalidInputRefsError(f"Invalid input_ref (expected {{'type': 'mainai_task', 'id': ...}}): {ref!r}")
    try:
        task_id = uuid.UUID(str(ref["id"]))
    except ValueError as exc:
        raise InvalidInputRefsError(f"Invalid mainai_task id in input_refs: {ref['id']!r}") from exc
    task = db.execute(select(MainAITask).where(MainAITask.id == task_id, MainAITask.owner_id == owner_id)).scalar_one_or_none()
    if task is None:
        raise InvalidInputRefsError(f"MainAITask {task_id} not found or not owned by this account.")
    if task.status != MainAITaskStatus.ready:
        raise InvalidInputRefsError(f"MainAITask {task_id} has status '{task.status.value}', not 'ready' — not a valid dispatch target.")


def create_job(
    db: Session,
    *,
    owner_id: uuid.UUID,
    job_type: str,
    input_refs: list[dict],
    created_by: str,
    idempotency_key: str | None = None,
    request=None,
) -> MainAIJob:
    """Fails closed (CapabilityUnavailableError) before creating any row if job_type is not
    on CAPABILITY_MANIFEST — the founder's explicit requirement, enforced here so no router
    or future caller can bypass it by calling this function directly.

    Idempotent per (owner_id, idempotency_key): a second call with the same key for the same
    owner returns the ORIGINAL job unchanged (no new row, no new event) — regardless of
    whether its job_type/input_refs differ from the first call's, deliberately: the key alone
    is the identity, exactly matching ImportJob's own source_checksum idempotency
    (app/models/import_job.py), which has no payload-mismatch detection either. A caller that
    needs a genuinely different job must use a different idempotency_key.

    Founder re-review round (PR #36): the ONLY safe way to make this idempotent under real
    concurrency — two requests with the same key racing each other, e.g. a client retrying
    after an HTTP timeout — is the same SAVEPOINT + real INSERT + catch the exact unique-
    violation + rollback-to-savepoint + fresh SELECT pattern already used by
    app/rag/memory_source.py::get_or_create_memory_source_unit() (see that function's own
    docstring for why a plain SELECT-then-INSERT, which this function used to do, cannot be
    made safe: it's a classic TOCTOU race — both callers can pass the SELECT before either
    commits its INSERT). Reproduced live during review: two real threads, two real sessions,
    same (owner_id, idempotency_key) — the loser previously got an UNHANDLED IntegrityError
    instead of the existing job.

    Fourth pass: the idempotency lookup now happens BEFORE require_capability(), not after —
    a replay under an EXISTING key must be recognized as a replay before anything about the
    (possibly since-changed, possibly irrelevant) capability of the NEW call's job_type is even
    considered; otherwise a caller retrying with the same key but a client-side bug that
    changed job_type could get a misleading CapabilityUnavailableError instead of either the
    original job (identical request) or a clear IdempotencyConflictError (different request) —
    see _canonical_request_fingerprint(). A genuinely new capability check (no matching key, or
    no key at all) still runs and still fails closed exactly as before."""
    fingerprint = _canonical_request_fingerprint(job_type, input_refs)

    if idempotency_key:
        existing = db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == owner_id, MainAIJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            if _canonical_request_fingerprint(existing.job_type, existing.input_refs) != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency_key '{idempotency_key}' was already used for a different request "
                    f"(job {existing.id}, job_type '{existing.job_type}')."
                )
            return existing

    require_capability(db, job_type)  # raises CapabilityUnavailableError — never caught here, the router maps it to 409

    if job_type == "corpus_review":
        _validate_input_refs(db, owner_id, input_refs)
    elif job_type == "message_sequence_backfill":
        # S1B: this job's scope is "every one of THIS owner's still-unnumbered messages",
        # derived from the database at execution time (app/jobs/handlers/message_sequence_backfill.py)
        # — it takes no inputs at all. Non-empty input_refs are rejected rather than silently
        # ignored: accepting refs the executor will never read would let a caller believe it had
        # narrowed the job's scope when it had not, which is exactly the kind of quiet
        # mismatch between what a job claims and what it does that the runtime-truthfulness
        # contract exists to prevent.
        if input_refs:
            raise InvalidInputRefsError("message_sequence_backfill takes no input_refs — its scope is the whole owner's unnumbered message history.")
    elif job_type == "task_execution":
        # MainAI Execution Loop V0.1 (app/mainai_execution/executor.py): exactly one
        # {"type": "mainai_task", "id": "..."} ref, pointing at a real, owner-visible
        # MainAITask row (RLS-enforced by the query itself, same as corpus_review's document
        # check above) — never more than one, since a task_execution job is always the
        # execution unit for exactly one task, and never fewer, since a task_execution job
        # with no task to execute has nothing to do.
        _validate_task_execution_input_refs(db, owner_id, input_refs)

    savepoint = db.begin_nested()
    try:
        job = MainAIJob(
            owner_id=owner_id,
            job_type=job_type,
            status=MainAIJobStatus.queued,
            input_refs=input_refs,
            output_refs=[],
            created_by=created_by,
            idempotency_key=idempotency_key,
        )
        db.add(job)
        db.flush()  # job.id is needed for the event row below
        _record_event(db, job, MainAIJobEventType.created, {"job_type": job_type, "input_refs": input_refs})
        # commit=False: record_audit()'s own default (commit=True) would end the OUTER
        # transaction here, not just this SAVEPOINT -- invalidating `savepoint` before its own
        # explicit .commit()/.rollback() below ever runs. The audit row becomes durable
        # together with everything else in this block via the plain db.commit() at the very
        # end of this function, same as every other write here.
        record_audit(db, user_id=owner_id, action="mainai_job_created", entity_type="mainai_job", entity_id=str(job.id), request=request, commit=False)
        savepoint.commit()
    except IntegrityError as exc:
        # Only the exact idempotency-key collision this function is designed to recover from
        # is treated as "someone else already created this" — any other IntegrityError (a
        # real bug, a different constraint, e.g. a bad owner_id FK) is re-raised unmodified
        # rather than silently misdiagnosed as an idempotency race, same reasoning as
        # get_or_create_memory_source_unit's identical guard.
        constraint_name = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
        savepoint.rollback()
        if constraint_name != "uq_mainai_jobs_owner_idempotency_key":
            raise
        existing = db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == owner_id, MainAIJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is None:
            # The row that won the race hasn't committed yet from THIS session's read view
            # (READ COMMITTED can't see an in-flight-but-not-yet-committed transaction) —
            # vanishingly unlikely to matter in practice (the winner's own INSERT+events
            # commit as one fast transaction) but re-raising the original conflict is more
            # honest than returning None or looping forever.
            raise
        if _canonical_request_fingerprint(existing.job_type, existing.input_refs) != fingerprint:
            raise IdempotencyConflictError(
                f"idempotency_key '{idempotency_key}' was already used for a different request "
                f"(job {existing.id}, job_type '{existing.job_type}')."
            ) from exc
        return existing

    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: uuid.UUID) -> MainAIJob:
    """RLS already ensures this can only ever return a job the current request's owner
    actually owns — a job_id belonging to a different owner simply does not exist from this
    session's point of view, so this correctly raises JobNotFoundError (mapped to HTTP 404,
    never 403) rather than leaking whether the id exists at all.

    populate_existing=True is required, not cosmetic: plain Session.get() returns straight
    from the identity map without re-querying if this exact (class, pk) was already loaded on
    this session — which would silently bypass RLS if the session's RLS owner context changes
    between two lookups of the same job_id (the worker loop and this module's own test suite
    both reuse one session across different RLS owner contexts). Forcing a real SELECT means
    Postgres's RLS policy is re-evaluated against the CURRENT app.current_user_id every time."""
    job = db.get(MainAIJob, job_id, populate_existing=True)
    if job is None:
        raise JobNotFoundError(str(job_id))
    return job


def list_jobs(db: Session, *, limit: int = 50, offset: int = 0) -> list[MainAIJob]:
    """Founder re-review round (PR #36), fourth pass: `created_at` alone is not a stable sort
    key for limit/offset pagination -- two jobs created within the same timestamp (realistic
    under fast/automated job creation) could shuffle order between page fetches, producing a
    duplicate or missing row across pages. `id` (a UUID, unique and immutable) as the secondary
    key makes the ordering total and deterministic regardless of `created_at` ties."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return list(
        db.execute(select(MainAIJob).order_by(MainAIJob.created_at.desc(), MainAIJob.id.desc()).limit(limit).offset(offset)).scalars()
    )


def list_job_events(db: Session, job_id: uuid.UUID, *, limit: int = 200) -> list[MainAIJobEvent]:
    get_job(db, job_id)  # raises JobNotFoundError under the same RLS scoping as above
    limit = max(1, min(limit, 1000))
    return list(
        db.execute(
            select(MainAIJobEvent).where(MainAIJobEvent.job_id == job_id).order_by(MainAIJobEvent.created_at.asc()).limit(limit)
        ).scalars()
    )


def request_cancel(db: Session, job_id: uuid.UUID, *, requested_by: uuid.UUID, request=None) -> MainAIJob:
    """Sets cancel_requested only — the actual transition to `cancelled` happens inside the
    job's own processing loop (app/jobs/handlers/corpus_review.py), between batches, so a job is
    never killed mid-write. Idempotent: calling this twice on an already-cancel-requested job
    is a no-op, not an error."""
    job = get_job(db, job_id)
    if job.status not in CANCELLABLE_MAINAI_JOB_STATUSES:
        raise InvalidJobTransitionError(f"Cannot cancel a job in status '{job.status.value}'.")
    if not job.cancel_requested:
        job.cancel_requested = True
        db.add(job)
        _record_event(db, job, MainAIJobEventType.cancel_requested)
        record_audit(db, user_id=requested_by, action="mainai_job_cancel_requested", entity_type="mainai_job", entity_id=str(job.id), request=request)
        db.commit()
        db.refresh(job)
    return job


def retry_job(db: Session, job_id: uuid.UUID, *, requested_by: uuid.UUID, request=None) -> MainAIJob:
    """Only a genuinely terminal `failed` job (never `cancelled` — see
    RETRYABLE_MAINAI_JOB_STATUSES's own docstring) within its retry budget can be retried.
    Resets progress fields so the corpus_review loop restarts its batch scan from the top —
    safe because it is itself idempotent per already-produced proposal (see
    app/jobs/handlers/corpus_review.py).

    Documented, deliberate scope of what this DOES and does NOT reset (founder re-review
    round, PR #36, fourth pass): `started_at`, `locked_by`, `lease_expires_at`,
    `last_heartbeat_at`, `lease_generation`, `provider`, and `model` are left exactly as the
    failed attempt left them. This is safe, not an oversight: while `status` is `queued`, every
    worker-driven guarded write requires `status = 'running'` (see `_guarded_job_write`), so
    these stale values can never be acted on by anyone; the next successful claim
    (`claim_next_mainai_job`) unconditionally overwrites `locked_by`/`lease_generation`/
    `lease_expires_at`/`last_heartbeat_at` with fresh values regardless of what they held
    before. The one visible consequence: `started_at` (`COALESCE(started_at, now())` at claim
    time) will keep reflecting the job's FIRST-EVER claim across every retry, not the most
    recent attempt — an explicit design choice ("when did work on this job begin at all"), not
    a bug, and now stated here rather than left implicit."""
    job = get_job(db, job_id)
    if job.status not in RETRYABLE_MAINAI_JOB_STATUSES:
        raise InvalidJobTransitionError(f"Cannot retry a job in status '{job.status.value}'.")
    if job.retry_count >= job.max_retries:
        raise InvalidJobTransitionError(f"Job {job.id} has exhausted its retry budget ({job.retry_count}/{job.max_retries}).")
    job.status = MainAIJobStatus.queued
    job.retry_count += 1
    job.error_category = None
    job.public_message = None
    job.cancel_requested = False
    job.cancel_acknowledged = False
    # completed_at must go back to NULL together with the status leaving its terminal set --
    # migration 0028's ck_mainai_jobs_completed_at_matches_terminal_status enforces this at
    # the DB level now (a real gap this constraint caught: this line was missing before the
    # founder re-review round added that CHECK). progress_current/progress_total/current_phase
    # reset too, matching this function's own long-standing docstring claim ("resets progress
    # fields") that the code itself didn't actually do -- corpus_review.py recomputes them
    # from scratch on its next run regardless, but leaving stale "3/5"-style progress visible
    # on a `queued` job between the retry and the next claim would be misleading in the admin
    # UI (app/routers/mainai_jobs.py's GET /jobs, frontend/app/(shell)/admin/jobs/page.tsx).
    job.completed_at = None
    job.progress_current = 0
    job.progress_total = None
    job.current_phase = None
    db.add(job)
    _record_event(db, job, MainAIJobEventType.retry_scheduled, {"retry_count": job.retry_count})
    record_audit(db, user_id=requested_by, action="mainai_job_retry", entity_type="mainai_job", entity_id=str(job.id), request=request)
    db.commit()
    db.refresh(job)
    return job


def record_claimed(db: Session, job: MainAIJob, *, worker_id: str, lease_generation: int) -> None:
    """Called by the worker (app/worker.py's process_claimed_mainai_job) immediately after
    claim_next_mainai_job() has already committed the status/locked_by/lease/lease_generation
    change via raw SQL (app/jobs/mainai_job_lease.py — that function runs on the superuser/
    migration connection, across every owner's rows at once, so it cannot itself write an
    owner-RLS-scoped MainAIJobEvent or an audit entry). This is the "start" half of the
    founder's create/start/cancel/retry/complete/fail audit requirement — it runs on the
    normal RLS-scoped session, once the worker has already re-scoped it to this job's owner.

    Still goes through the same fencing gate as every other worker-driven write below (a
    harmless self-assignment as the SET clause — there is nothing to change, only to verify)
    rather than blindly trusting the (worker_id, lease_generation) claim_next_mainai_job just
    returned: consistency with every other call site here matters more than the few
    microseconds saved by special-casing "this one is called right after claiming so it can't
    possibly be stale" — a future refactor that reorders these calls should not silently
    reintroduce the exact bug this module exists to close."""
    _guarded_job_write(db, job.id, worker_id=worker_id, lease_generation=lease_generation, set_sql="locked_by = locked_by", params={})
    _record_event(db, job, MainAIJobEventType.claimed, {"worker_id": worker_id})
    record_audit(db, user_id=job.owner_id, action="mainai_job_claimed", entity_type="mainai_job", entity_id=str(job.id), detail=worker_id)
    db.commit()


def mark_completed(db: Session, job: MainAIJob, *, worker_id: str, lease_generation: int, public_message: str | None = None) -> None:
    _guarded_job_write(
        db,
        job.id,
        worker_id=worker_id,
        lease_generation=lease_generation,
        set_sql="status = 'completed', completed_at = :completed_at, public_message = :message",
        params={"completed_at": datetime.utcnow(), "message": public_message or "Completed."},
    )
    db.refresh(job)
    _record_event(db, job, MainAIJobEventType.completed)
    record_audit(db, user_id=job.owner_id, action="mainai_job_completed", entity_type="mainai_job", entity_id=str(job.id))
    db.commit()


def mark_failed(db: Session, job: MainAIJob, *, worker_id: str, lease_generation: int, error_category: MainAIJobErrorCategory) -> None:
    """`error_category` is the ONLY thing derived from the real failure — public_message
    always comes from the fixed _PUBLIC_ERROR_MESSAGES table above, never from the exception
    itself. Callers are expected to log the real exception separately (see
    app/jobs/handlers/corpus_review.py) — this function only ever persists the safe category."""
    _guarded_job_write(
        db,
        job.id,
        worker_id=worker_id,
        lease_generation=lease_generation,
        set_sql="status = 'failed', completed_at = :completed_at, error_category = :category, public_message = :message",
        params={
            "completed_at": datetime.utcnow(),
            "category": error_category.value,
            "message": _PUBLIC_ERROR_MESSAGES[error_category],
        },
    )
    db.refresh(job)
    _record_event(db, job, MainAIJobEventType.failed, {"error_category": error_category.value})
    record_audit(db, user_id=job.owner_id, action="mainai_job_failed", entity_type="mainai_job", entity_id=str(job.id), detail=error_category.value)
    db.commit()


def mark_cancelled(db: Session, job: MainAIJob, *, worker_id: str, lease_generation: int) -> None:
    _guarded_job_write(
        db,
        job.id,
        worker_id=worker_id,
        lease_generation=lease_generation,
        set_sql="status = 'cancelled', completed_at = :completed_at, cancel_acknowledged = true, public_message = :message",
        params={"completed_at": datetime.utcnow(), "message": _PUBLIC_ERROR_MESSAGES[MainAIJobErrorCategory.cancelled_by_owner]},
    )
    db.refresh(job)
    _record_event(db, job, MainAIJobEventType.cancel_acknowledged)
    _record_event(db, job, MainAIJobEventType.cancelled)
    record_audit(db, user_id=job.owner_id, action="mainai_job_cancelled", entity_type="mainai_job", entity_id=str(job.id))
    db.commit()


def update_progress(
    db: Session,
    job: MainAIJob,
    *,
    worker_id: str,
    lease_generation: int,
    current: int | None = None,
    total: int | None = None,
    phase: str | None = None,
) -> None:
    """Called between batches by the job's own processing loop. Deliberately does NOT commit
    — the caller's own batch transaction decides the commit boundary (see
    app/jobs/handlers/corpus_review.py), so progress is only ever durable together with the work it
    describes, never ahead of it.

    Requires and re-verifies (worker_id, lease_generation) via the same fencing gate as every
    other worker-driven write — see _guarded_job_write's docstring. A no-op call (nothing to
    set) is not fenced, since it changes nothing about the row."""
    phase_changed = phase is not None and phase != job.current_phase
    set_parts: list[str] = []
    params: dict = {}
    if current is not None:
        set_parts.append("progress_current = :current")
        params["current"] = current
    if total is not None:
        set_parts.append("progress_total = :total")
        params["total"] = total
    if phase is not None:
        set_parts.append("current_phase = :phase")
        params["phase"] = phase
    if not set_parts:
        return
    _guarded_job_write(db, job.id, worker_id=worker_id, lease_generation=lease_generation, set_sql=", ".join(set_parts), params=params)
    db.refresh(job)
    if phase_changed:
        _record_event(db, job, MainAIJobEventType.phase_changed, {"phase": phase})
    _record_event(db, job, MainAIJobEventType.progress_updated, {"current": job.progress_current, "total": job.progress_total})


def record_document_reviewed(
    db: Session,
    job: MainAIJob,
    *,
    worker_id: str,
    lease_generation: int,
    current: int,
    total: int,
    provider: str,
    model: str,
    proposal_output_ref: dict,
) -> None:
    """One successful per-document `corpus_review` outcome (app/jobs/handlers/corpus_review.py):
    progress advances, provider/model attribution updates, and the new proposal's reference is
    appended to `output_refs` — all in the SAME fencing-guarded UPDATE (`output_refs || ...`
    is Postgres's own jsonb array-concatenation operator, evaluated inside the guarded
    statement, never read-modify-written in Python), so a stale worker's "success" can never
    partially land (e.g. output_refs updated but progress not, or vice versa) between a
    lease-loss check and the write. Only the guarded columns on `mainai_jobs` are covered here
    — the `MainAIJobProposal` row itself is a separate INSERT the caller makes in the SAME
    transaction as this call, which only commits together with it."""
    result = db.execute(
        text(
            "UPDATE mainai_jobs SET progress_current = :current, progress_total = :total, "
            "current_phase = 'reviewing', provider = :provider, model = :model, "
            "output_refs = output_refs || CAST(:output_ref AS jsonb) "
            "WHERE id = :job_id AND locked_by = :worker_id AND lease_generation = :lease_generation AND status = 'running'"
        ),
        {
            "current": current,
            "total": total,
            "provider": provider,
            "model": model,
            "output_ref": json.dumps([proposal_output_ref]),
            "job_id": str(job.id),
            "worker_id": worker_id,
            "lease_generation": lease_generation,
        },
    )
    if result.rowcount == 0:
        raise JobLeaseLostError(job.id, worker_id, lease_generation)
    db.refresh(job)
    _record_event(db, job, MainAIJobEventType.progress_updated, {"current": current, "total": total})


def record_document_skipped(
    db: Session,
    job: MainAIJob,
    *,
    worker_id: str,
    lease_generation: int,
    document_id: uuid.UUID,
    reason: str,
    error_category: str | None = None,
) -> None:
    """One per-document `corpus_review` outcome that was NOT a successful review — deleted
    mid-run, no reviewable content, or a provider failure specific to this one document (see
    corpus_review.py's three call sites for the exact `reason` values; migration 0028
    added `document_skipped` to mainai_job_events' allowed event types for exactly this).
    Verifies the caller's fencing token via the same guarded gate as every other worker-driven
    write (a harmless self-assignment as the SET clause, matching record_claimed's reasoning
    for why even a "nothing to change on the job row" write still goes through the gate) before
    ever inserting the event — a stale worker must not be able to write ANY event, including
    this one, as if it still owned the job. `detail` never carries raw exception text, only the
    closed-vocabulary `reason` and (for a provider failure) the same safe `error_category`
    mark_failed() would use — never str(exception).

    Founder re-review round (PR #36), fourth pass: skip events are NOT deduplicated across
    retries the way proposals are (`_already_reviewed_document_ids()` only tracks documents
    with a PROPOSAL, never a skip) — a document skipped on attempt 1 that's skipped again on
    attempt 2 (after a retry) gets a second `document_skipped` event. This is intentional, not
    a bug: each event is a true record of what happened during that specific attempt. `detail`
    now includes `job.retry_count` (the attempt number this event happened during) so the two
    events can be told apart in the job's history instead of looking like an unexplained
    duplicate."""
    _guarded_job_write(db, job.id, worker_id=worker_id, lease_generation=lease_generation, set_sql="locked_by = locked_by", params={})
    detail = {"document_id": str(document_id), "reason": reason, "attempt": job.retry_count}
    if error_category is not None:
        detail["error_category"] = error_category
    _record_event(db, job, MainAIJobEventType.document_skipped, detail)


__all__ = [
    "CapabilityUnavailableError",
    "IdempotencyConflictError",
    "InvalidInputRefsError",
    "InvalidJobTransitionError",
    "JobLeaseLostError",
    "JobNotFoundError",
    "create_job",
    "get_job",
    "list_job_events",
    "list_jobs",
    "mark_cancelled",
    "mark_completed",
    "mark_failed",
    "record_claimed",
    "record_document_reviewed",
    "record_document_skipped",
    "request_cancel",
    "retry_job",
    "update_progress",
]
