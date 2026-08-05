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

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
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


def _record_event(db: Session, job: MainAIJob, event_type: MainAIJobEventType, detail: dict | None = None) -> MainAIJobEvent:
    event = MainAIJobEvent(job_id=job.id, owner_id=job.owner_id, event_type=event_type, detail=detail or {})
    db.add(event)
    return event


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
    owner returns the ORIGINAL job unchanged (no new row, no new event) rather than creating a
    duplicate — mirrors ImportJob's source_checksum idempotency (app/models/import_job.py)."""
    require_capability(job_type)  # raises CapabilityUnavailableError — never caught here, the router maps it to 409

    if idempotency_key:
        existing = db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == owner_id, MainAIJob.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    if job_type == "corpus_review":
        _validate_input_refs(db, owner_id, input_refs)

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
    record_audit(db, user_id=owner_id, action="mainai_job_created", entity_type="mainai_job", entity_id=str(job.id), request=request)
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
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    return list(db.execute(select(MainAIJob).order_by(MainAIJob.created_at.desc()).limit(limit).offset(offset)).scalars())


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
    job's own processing loop (app/rag/corpus_review_job.py), between batches, so a job is
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
    app/rag/corpus_review_job.py)."""
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
    db.add(job)
    _record_event(db, job, MainAIJobEventType.retry_scheduled, {"retry_count": job.retry_count})
    record_audit(db, user_id=requested_by, action="mainai_job_retry", entity_type="mainai_job", entity_id=str(job.id), request=request)
    db.commit()
    db.refresh(job)
    return job


def record_claimed(db: Session, job: MainAIJob, *, worker_id: str) -> None:
    """Called by the worker (app/worker.py's process_claimed_mainai_job) immediately after
    claim_next_mainai_job() has already committed the status/locked_by/lease change via raw
    SQL (app/jobs/mainai_job_lease.py — that function runs on the superuser/migration
    connection, across every owner's rows at once, so it cannot itself write an
    owner-RLS-scoped MainAIJobEvent or an audit entry). This is the "start" half of the
    founder's create/start/cancel/retry/complete/fail audit requirement — it runs on the
    normal RLS-scoped session, once the worker has already re-scoped it to this job's owner."""
    _record_event(db, job, MainAIJobEventType.claimed, {"worker_id": worker_id})
    record_audit(db, user_id=job.owner_id, action="mainai_job_claimed", entity_type="mainai_job", entity_id=str(job.id), detail=worker_id)
    db.commit()


def mark_completed(db: Session, job: MainAIJob, *, public_message: str | None = None) -> None:
    job.status = MainAIJobStatus.completed
    job.completed_at = datetime.utcnow()
    job.public_message = public_message or "Completed."
    db.add(job)
    _record_event(db, job, MainAIJobEventType.completed)
    record_audit(db, user_id=job.owner_id, action="mainai_job_completed", entity_type="mainai_job", entity_id=str(job.id))
    db.commit()


def mark_failed(db: Session, job: MainAIJob, *, error_category: MainAIJobErrorCategory) -> None:
    """`error_category` is the ONLY thing derived from the real failure — public_message
    always comes from the fixed _PUBLIC_ERROR_MESSAGES table above, never from the exception
    itself. Callers are expected to log the real exception separately (see
    app/rag/corpus_review_job.py) — this function only ever persists the safe category."""
    job.status = MainAIJobStatus.failed
    job.completed_at = datetime.utcnow()
    job.error_category = error_category.value
    job.public_message = _PUBLIC_ERROR_MESSAGES[error_category]
    db.add(job)
    _record_event(db, job, MainAIJobEventType.failed, {"error_category": error_category.value})
    record_audit(db, user_id=job.owner_id, action="mainai_job_failed", entity_type="mainai_job", entity_id=str(job.id), detail=error_category.value)
    db.commit()


def mark_cancelled(db: Session, job: MainAIJob) -> None:
    job.status = MainAIJobStatus.cancelled
    job.completed_at = datetime.utcnow()
    job.cancel_acknowledged = True
    job.public_message = _PUBLIC_ERROR_MESSAGES[MainAIJobErrorCategory.cancelled_by_owner]
    db.add(job)
    _record_event(db, job, MainAIJobEventType.cancel_acknowledged)
    _record_event(db, job, MainAIJobEventType.cancelled)
    record_audit(db, user_id=job.owner_id, action="mainai_job_cancelled", entity_type="mainai_job", entity_id=str(job.id))
    db.commit()


def update_progress(
    db: Session,
    job: MainAIJob,
    *,
    current: int | None = None,
    total: int | None = None,
    phase: str | None = None,
) -> None:
    """Called between batches by the job's own processing loop. Deliberately does NOT commit
    — the caller's own batch transaction decides the commit boundary (see
    app/rag/corpus_review_job.py), so progress is only ever durable together with the work it
    describes, never ahead of it."""
    phase_changed = phase is not None and phase != job.current_phase
    if current is not None:
        job.progress_current = current
    if total is not None:
        job.progress_total = total
    if phase is not None:
        job.current_phase = phase
    db.add(job)
    if phase_changed:
        _record_event(db, job, MainAIJobEventType.phase_changed, {"phase": phase})
    _record_event(db, job, MainAIJobEventType.progress_updated, {"current": job.progress_current, "total": job.progress_total})


__all__ = [
    "CapabilityUnavailableError",
    "InvalidInputRefsError",
    "InvalidJobTransitionError",
    "JobNotFoundError",
    "create_job",
    "get_job",
    "list_job_events",
    "list_jobs",
    "mark_cancelled",
    "mark_completed",
    "mark_failed",
    "request_cancel",
    "retry_job",
    "update_progress",
]
