import os
import shutil
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, field_validator
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.jobs.heartbeat import worker_process_alive
from app.limiter import limiter
from app.models.document import ActiveTruthStatus, Document, IndexStatus, KnowledgeClassification
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.media_url_import import MediaUrlImport
from app.models.source_relationship import SourceRelationship
from app.models.user import User
from app.providers.registry import resolve_active
from app.providers.verification import classify_provider_exception
from app.rag.library_import import maybe_purge_blob
from app.rag.trust import assess_claim_confidence
from app.rag.vector_store import hybrid_search
from app.storage import get_storage
from app.storage.base import StorageError, StorageSizeLimitExceeded
from app.storage.purge import SourcePurgeNotFoundError, purge_source
from app.storage.references import (
    acquire_owner_erasure_lock,
    acquire_storage_key_lock,
    delete_if_unreferenced,
    get_storage_cleanup_ops_status,
)
from app.schemas import (
    DeleteConfirmIn,
    ImportJobOut,
    KnowledgeClaimOut,
    KnowledgeSourceDetailOut,
    KnowledgeSourceOut,
    LibrarySearchResponseOut,
    MediaSegmentOut,
    MediaUrlImportIn,
    MediaUrlImportOut,
    OpsStatusOut,
    SourceRelationshipIn,
    SourceRelationshipOut,
)

router = APIRouter(prefix="/api/library", tags=["library"], dependencies=[Depends(require_founder)])
settings = get_settings()

# Raw upload byte cap — deliberately distinct from zip_import.py's MAX_TOTAL_UNCOMPRESSED_BYTES
# (200 MB): that limit bounds what a ZIP is allowed to expand to; this one bounds the
# request body itself (a compressed archive is normally far smaller than its contents), and
# also covers a single non-ZIP file upload through this same endpoint.
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
CHUNK_PREVIEW_COUNT = 3
CHUNK_PREVIEW_LENGTH = 320


class LibrarySearchQuery(BaseModel):
    query: str
    top_k: int = 10
    project_id: uuid.UUID | None = None
    classification: str | None = None
    active_truth_status: str | None = None

    @field_validator("query")
    @classmethod
    def non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Sökfrågan får inte vara tom.")
        if len(v) > 2000:
            raise ValueError("Sökfrågan är för lång (max 2000 tecken).")
        return v

    @field_validator("top_k")
    @classmethod
    def bounded_top_k(cls, v: int) -> int:
        return max(1, min(v, 50))

    @field_validator("classification")
    @classmethod
    def valid_classification(cls, v: str | None) -> str | None:
        if v is not None and v not in {c.value for c in KnowledgeClassification}:
            raise ValueError("Ogiltig klassificering.")
        return v

    @field_validator("active_truth_status")
    @classmethod
    def valid_truth_status(cls, v: str | None) -> str | None:
        if v is not None and v not in {s.value for s in ActiveTruthStatus}:
            raise ValueError("Ogiltig sanningsstatus.")
        return v


def _visible_document_query(db: Session, owner_id: uuid.UUID):
    # RLS already scopes this to owner_id — the explicit filter is defense in depth (same
    # convention as app/rag/vector_store.py's search()), and deleted_at exclusion is a real
    # requirement, not redundant: RLS has no concept of "soft deleted", only "whose row is
    # this".
    return db.query(Document).filter(Document.uploaded_by == owner_id, Document.deleted_at.is_(None))


@router.post("/import", response_model=ImportJobOut)
@limiter.limit(f"{settings.rate_limit_library_import_per_minute}/minute")
async def import_package(
    request: Request,
    file: UploadFile,
    project_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Life Library durable-worker package: streams the upload straight to durable storage
    (app/storage/), computing size and sha256 as it goes — never buffers the whole file in
    memory (no `await file.read()` on the full body). Responds with the job id as soon as the
    original is safely, verifiedly stored; app/worker.py's poll loop does the actual
    extraction/indexing work asynchronously from here on, so this request never blocks on
    that (see app/rag/library_import.py's module docstring for the full architecture).

    Pass 26: acquires the owner-scoped erasure advisory lock (see app/storage/references.py's
    acquire_owner_erasure_lock docstring for the full race this closes against a concurrent
    DELETE /api/account) as the VERY FIRST thing this handler does — before anything else,
    including `storage.write_stream()` below, which is exactly what makes the lock effective:
    a concurrent account erasure either fully commits (and this request's own re-check just
    below then correctly refuses to proceed) or is still blocked behind this request's own
    hold on the lock (and so cannot yet have inventoried this request's not-yet-written blob).
    Re-verifies the owner still exists immediately after acquiring the lock — relying on the
    later `ImportJob.owner_id` foreign key to reject an insert AFTER the blob was already
    physically written would leave an orphaned, DB-unreferenced file on disk; this check fails
    closed BEFORE any bytes are written at all."""
    acquire_owner_erasure_lock(db, user.id)
    if db.query(User).filter_by(id=user.id).first() is None:
        raise HTTPException(status_code=401, detail="Kontot finns inte längre.")

    if project_id is not None:
        from app.models.project import Project

        if db.query(Project).filter_by(id=project_id).first() is None:
            raise HTTPException(status_code=404, detail="Projektet hittades inte.")

    storage = get_storage()

    def _read_chunk() -> bytes:
        return file.file.read(1 << 20)

    try:
        # The whole streaming write (chunk reads from the incoming multipart body AND writes
        # to the destination temp file) runs in a worker thread — run_in_threadpool, not a
        # bare call — so this sync I/O never blocks the event loop other requests share.
        blob = await run_in_threadpool(storage.write_stream, _read_chunk, max_bytes=MAX_UPLOAD_BYTES)
    except StorageSizeLimitExceeded:
        raise HTTPException(status_code=413, detail=f"Filen är för stor (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=f"Kunde inte lagra filen: {exc}")

    if blob.size_bytes == 0:
        # Pass 30 (a fourth founder review round): a bare `storage.delete()` here used to
        # bypass the canonical check-then-act protocol entirely -- see
        # app/storage/references.py's delete_if_unreferenced() for the full incident this
        # closes. Content-addressing means EVERY empty upload shares the exact same
        # storage_key, so an ungated delete here could physically destroy an unrelated,
        # already-live reference (a Document, an ImportJob, or founder-wide Project Memory)
        # to that same empty-content key. The outcome doesn't change this request's own
        # response -- an empty upload is always rejected -- it only decides whether the
        # physical bytes are safe to remove.
        delete_if_unreferenced(db, storage, blob.storage_key)
        # No DB row was ever written in this request -- rolls back to release the owner-
        # erasure lock (acquired at the top of this handler) and the storage-key lock
        # (acquired inside delete_if_unreferenced above) immediately, rather than relying on
        # get_db()'s dependency-teardown close() to do it implicitly and later.
        db.rollback()
        raise HTTPException(status_code=400, detail="Filen är tom.")

    checksum = blob.sha256

    # Whole-upload idempotency: re-uploading byte-identical content (the same ZIP or the
    # same single file) returns the original completed job instead of a new one — see
    # docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md. A job that's still pending/running/failed for
    # this checksum is NOT treated as a duplicate: pending/running could be a legitimate
    # concurrent retry, and failed should be retriable, not permanently stuck as "the"
    # result for this checksum.
    existing = (
        db.query(ImportJob)
        .filter_by(owner_id=user.id, source_checksum=checksum, status=ImportJobStatus.completed)
        .order_by(ImportJob.completed_at.desc())
        .first()
    )
    if existing is not None:
        # ...but only if that job's result is still actually there. A founder who deletes a
        # source and then re-imports the identical file must get a real re-import, not a
        # phantom "completed" response pointing at documents that no longer exist — found via
        # STEG 14's full vertical review, reproduced by a fresh import immediately following a
        # delete of the same content. Without this check the UI confidently shows "completed,
        # 1 importerade" while the library silently stays empty.
        still_has_result = (
            db.query(Document.id)
            .filter(Document.import_job_id == existing.id, Document.deleted_at.is_(None))
            .first()
            is not None
        )
        if still_has_result:
            return existing

    # Pass 22: storage.write_stream() above already made this blob's bytes durable on disk,
    # but nothing in the database references it yet — a concurrent retry_source_blob_purge()
    # could run its own reference check in exactly this window, see nothing pointing at this
    # key, and physically delete it before the ImportJob row below is even created. Acquiring
    # the SAME storage_key-scoped advisory lock retry_source_blob_purge() takes closes that
    # race: whichever side gets here first fully commits or rolls back before the other's own
    # check can run (see app/storage/references.py's module docstring). The lock is
    # transaction-scoped — released automatically at this request's eventual commit below (or
    # at rollback, if the HTTPException just below fires).
    acquire_storage_key_lock(db, blob.storage_key)
    if not storage.exists(blob.storage_key):
        # Lost the race: a concurrent purge deleted this exact blob in the window between
        # write_stream() finishing and this lock being acquired. There is no safe way to
        # recreate the original bytes here — the incoming stream has already been fully read
        # and discarded (see LocalFilesystemStorage.write_stream) — so this fails closed
        # rather than ever committing an ImportJob whose source_storage_key points at
        # nothing. A retried upload re-runs write_stream() and gets a fresh, real blob.
        raise HTTPException(
            status_code=409,
            detail="Uppladdningen kolliderade med en samtidig radering av en identisk fil. Försök igen.",
        )

    job = ImportJob(
        owner_id=user.id,
        project_id=project_id,
        status=ImportJobStatus.pending,
        source_filename=file.filename,
        source_checksum=checksum,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
        source_media_type=file.content_type,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    # No BackgroundTasks call here anymore — app/worker.py's poll loop picks up `pending`
    # jobs on its own, restart-safe (see that module's docstring). The original is already
    # durably stored by the time this response is sent, regardless of what happens next.
    return job


RECENT_JOBS_LIMIT = 50


@router.get("/jobs", response_model=list[ImportJobOut])
def list_import_jobs(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Life Library upload consolidation package (DEL 3): lets the frontend's upload queue
    recover real server job state after a page reload or a fresh login — both pending/running
    (still in-flight) and recently finished jobs are returned, newest first, so the client
    never has to guess or show 'completed' before the server actually says so."""
    return (
        db.query(ImportJob)
        .filter_by(owner_id=user.id)
        .order_by(ImportJob.created_at.desc())
        .limit(RECENT_JOBS_LIMIT)
        .all()
    )


@router.get("/jobs/{job_id}", response_model=ImportJobOut)
def get_import_job(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    job = db.query(ImportJob).filter_by(id=job_id, owner_id=user.id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Importjobbet hittades inte.")
    return job


@router.post("/import-url", response_model=MediaUrlImportOut)
@limiter.limit(f"{settings.rate_limit_library_import_per_minute}/minute")
async def create_media_url_import(
    request: Request,
    payload: MediaUrlImportIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """STEG 12's secure URL-import MODEL — records the founder's intent to import a URL
    (e.g. a future YouTube/web video) without ever fetching it. See
    app/models/media_url_import.py's docstring: nothing reads this row's `url` and makes a
    network request anywhere in this codebase. The row starts and stays `pending_review` —
    there is deliberately no endpoint that advances it, since actually fetching would require
    a reviewed, rights-aware fetcher this work order explicitly does not build."""
    if payload.project_id is not None:
        from app.models.project import Project

        if db.query(Project).filter_by(id=payload.project_id).first() is None:
            raise HTTPException(status_code=404, detail="Projektet hittades inte.")

    record = MediaUrlImport(
        owner_id=user.id,
        project_id=payload.project_id,
        url=payload.url,
        platform=payload.platform,
        consent_confirmed=payload.consent_confirmed,
        rights_note=payload.rights_note,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/url-imports", response_model=list[MediaUrlImportOut])
def list_media_url_imports(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return db.query(MediaUrlImport).filter_by(owner_id=user.id).order_by(MediaUrlImport.created_at.desc()).all()


@router.get("", response_model=list[KnowledgeSourceOut])
def list_sources(
    project_id: uuid.UUID | None = None,
    classification: str | None = None,
    active_truth_status: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    query = _visible_document_query(db, user.id)
    if project_id is not None:
        query = query.filter(Document.project_id == project_id)
    if classification is not None:
        query = query.filter(Document.classification == classification)
    if active_truth_status is not None:
        query = query.filter(Document.active_truth_status == active_truth_status)
    if q:
        pattern = f"%{q.strip()[:200]}%"
        query = query.filter(or_(Document.title.ilike(pattern), Document.original_filename.ilike(pattern)))
    return query.order_by(Document.created_at.desc()).all()


@router.get("/{source_id}", response_model=KnowledgeSourceDetailOut)
def get_source(source_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    document = _visible_document_query(db, user.id).filter(Document.id == source_id).first()
    if document is None:
        raise HTTPException(status_code=404, detail="Källan hittades inte.")

    versions = db.query(KnowledgeVersion).filter_by(source_id=source_id).order_by(KnowledgeVersion.version_number.desc()).all()
    relationships = (
        db.query(SourceRelationship)
        .filter(or_(SourceRelationship.from_source_id == source_id, SourceRelationship.to_source_id == source_id))
        .order_by(SourceRelationship.created_at.desc())
        .all()
    )
    chunks = (
        db.query(DocumentChunk)
        .filter_by(document_id=source_id)
        .order_by(DocumentChunk.chunk_index.asc())
        .limit(CHUNK_PREVIEW_COUNT)
        .all()
    )
    claims = db.query(KnowledgeClaim).filter_by(source_id=source_id).order_by(KnowledgeClaim.created_at.asc()).all()

    detail = KnowledgeSourceDetailOut.model_validate(document)
    detail.versions = versions
    detail.relationships = relationships
    detail.chunk_preview = [c.text[:CHUNK_PREVIEW_LENGTH] for c in chunks]
    # confidence is recomputed live (assess_claim_confidence), not read from the stored
    # extraction-time value — see app/rag/trust.py's docstring for why (a relationship added
    # after extraction must be reflected immediately).
    detail.claims = [
        KnowledgeClaimOut(
            id=c.id,
            claim_text=c.claim_text,
            claim_type=c.claim_type.value,
            status=c.status.value,
            confidence=assess_claim_confidence(db, c).value,
            grounding_score=c.grounding_score,
            chunk_id=c.chunk_id,
            created_at=c.created_at,
        )
        for c in claims
    ]
    # STEG 13: the FULL timestamped chunk list, not just the CHUNK_PREVIEW_COUNT preview
    # above — only meaningful for a media source (media_duration_seconds is None for every
    # text/document import), so this stays [] for those, exactly like before this field
    # existed.
    if document.media_duration_seconds is not None:
        all_chunks = db.query(DocumentChunk).filter_by(document_id=source_id).order_by(DocumentChunk.chunk_index.asc()).all()
        detail.segments = [
            MediaSegmentOut(chunk_index=c.chunk_index, text=c.text, start_seconds=c.start_seconds, end_seconds=c.end_seconds)
            for c in all_chunks
        ]
    return detail


@router.get(
    "/{source_id}/media",
    # response_class=Response (not the default JSONResponse) is what stops FastAPI from
    # ALSO adding its own empty application/json entry alongside the real content types
    # below — without it, responses= merges with rather than replaces FastAPI's default
    # JSON assumption, per FastAPI's own documented behavior.
    response_class=Response,
    responses={
        # Without this, FastAPI's default OpenAPI generation assumes every route returns
        # application/json (an empty {} schema, since it has no way to infer the real type
        # from a runtime-determined media_type= on a plain Response) — actively misleading
        # for a route whose entire point is to return raw audio/video bytes. Declaring the
        # real content types here is what tests/backend/test_openapi_schema.py's
        # test_media_route_is_not_falsely_documented_as_json checks for.
        200: {"content": {"audio/mpeg": {}, "video/mp4": {}}, "description": "Rå ljud-/videobytes för uppspelning."},
    },
)
def get_source_media(source_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """STEG 13: streams the raw bytes an audio/video import kept (Document.media_blob, see
    app/rag/media_import.py) back to an <audio>/<video> element. Same RLS-scoped,
    deleted_at-excluding query every other source-detail route uses — a deleted or
    someone-else's source 404s here exactly like it does everywhere else in this router, not
    just a "hidden from listings" soft block."""
    document = _visible_document_query(db, user.id).filter(Document.id == source_id).first()
    if document is None or document.media_blob is None:
        raise HTTPException(status_code=404, detail="Ingen mediefil hittades för den här källan.")
    return Response(content=document.media_blob, media_type=document.media_type or "application/octet-stream")


@router.delete("/{source_id}")
def delete_source(
    source_id: uuid.UUID,
    payload: DeleteConfirmIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Thin wrapper: validates the request (confirmation required) and delegates all actual
    deletion/purge logic to the shared app/storage/purge.py::purge_source() service — see
    that module's docstring for the full behavior (soft delete, chunk purge, S1A memory-source
    purge, blob release, source_purged audit entry — all one atomic commit as of Pass 22). The
    still-live older `DELETE /api/documents/{id}` (app/routers/documents.py) calls the exact
    same service — never a second, diverging implementation. The router's only job re: the
    audit trail is extracting a neutral IP string from the request — purge_source() itself
    never imports fastapi."""
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Radering kräver explicit bekräftelse (confirm: true).")

    client_ip = request.client.host if request.client else None
    try:
        purge_source(db, source_id, user.id, client_ip=client_ip)
    except SourcePurgeNotFoundError:
        raise HTTPException(status_code=404, detail="Källan hittades inte.")

    return {"status": "deleted"}


@router.get("/ops/status", response_model=OpsStatusOut)
def ops_status(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Life Library durable-worker package (DEL 6): founder-only visibility into whether the
    worker is actually alive and keeping up — no private paths or secrets (see
    OpsStatusOut's docstring), just aggregated numbers a founder can act on ('the worker
    looks stuck, go check the container')."""
    now = datetime.utcnow()

    queue_length = db.query(ImportJob).filter_by(owner_id=user.id, status=ImportJobStatus.pending).count()
    running_jobs = db.query(ImportJob).filter_by(owner_id=user.id, status=ImportJobStatus.running).count()

    oldest_pending_row = (
        db.query(ImportJob.created_at)
        .filter_by(owner_id=user.id, status=ImportJobStatus.pending)
        .order_by(ImportJob.created_at.asc())
        .first()
    )
    oldest_pending_age_seconds = (now - oldest_pending_row[0]).total_seconds() if oldest_pending_row else None

    since = now - timedelta(hours=24)
    failed_last_24h = (
        db.query(ImportJob)
        .filter(ImportJob.owner_id == user.id, ImportJob.status == ImportJobStatus.failed, ImportJob.completed_at >= since)
        .count()
    )

    last_heartbeat_row = (
        db.query(ImportJob.last_heartbeat_at)
        .filter(ImportJob.owner_id == user.id, ImportJob.last_heartbeat_at.isnot(None))
        .order_by(ImportJob.last_heartbeat_at.desc())
        .first()
    )
    last_heartbeat_at = last_heartbeat_row[0] if last_heartbeat_row else None
    # 2026-07-28: this ImportJob-based check only ever proves a worker claimed a job
    # recently — a worker with nothing currently claimable never touches
    # last_heartbeat_at at all, so on its own this under-reports a healthy-but-idle worker
    # as unreachable. app/jobs/heartbeat.py's process-level Redis signal (written every poll
    # cycle regardless of whether a job was claimed) is checked first when available; this
    # older check remains the fallback for when that signal itself is unavailable.
    worker_alive = worker_process_alive()
    if worker_alive is not None:
        worker_reachable = worker_alive
    else:
        worker_reachable = last_heartbeat_at is not None and (now - last_heartbeat_at) < timedelta(
            seconds=settings.worker_lease_seconds * 3
        )

    storage_writable = os.access(settings.storage_root, os.W_OK) if os.path.isdir(settings.storage_root) else False
    try:
        free_disk_bytes = shutil.disk_usage(settings.storage_root).free
    except OSError:
        free_disk_bytes = None

    storage_cleanup = get_storage_cleanup_ops_status(db)

    return OpsStatusOut(
        worker_reachable=worker_reachable,
        queue_length=queue_length,
        running_jobs=running_jobs,
        oldest_pending_age_seconds=oldest_pending_age_seconds,
        failed_last_24h=failed_last_24h,
        storage_writable=storage_writable,
        free_disk_bytes=free_disk_bytes,
        last_heartbeat_at=last_heartbeat_at,
        storage_cleanup_degraded=storage_cleanup.storage_cleanup_degraded,
        storage_orphan_risk_count=storage_cleanup.storage_orphan_risk_count,
        latest_storage_orphan_risk_at=storage_cleanup.latest_storage_orphan_risk_at,
        pending_storage_cleanup_tasks=storage_cleanup.pending_storage_cleanup_tasks,
        failed_storage_cleanup_tasks=storage_cleanup.failed_storage_cleanup_tasks,
        oldest_failed_storage_cleanup_age_seconds=storage_cleanup.oldest_failed_storage_cleanup_age_seconds,
    )


@router.post("/{source_id}/relationships", response_model=SourceRelationshipOut)
def create_relationship(
    source_id: uuid.UUID,
    payload: SourceRelationshipIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    if source_id == payload.to_source_id:
        raise HTTPException(status_code=400, detail="En källa kan inte relatera till sig själv.")
    from_doc = _visible_document_query(db, user.id).filter(Document.id == source_id).first()
    if from_doc is None:
        raise HTTPException(status_code=404, detail="Källan hittades inte.")
    # Both ends must belong to the current owner — RLS already guarantees this for anything
    # actually reachable, but an explicit check here gives a clean 404 for "the other source
    # doesn't exist (or isn't yours)" instead of a confusing FK-constraint failure. See
    # app/models/source_relationship.py's docstring.
    to_doc = _visible_document_query(db, user.id).filter(Document.id == payload.to_source_id).first()
    if to_doc is None:
        raise HTTPException(status_code=404, detail="Målkällan hittades inte.")

    relationship = SourceRelationship(
        owner_id=user.id,
        from_source_id=source_id,
        to_source_id=payload.to_source_id,
        relationship_type=payload.relationship_type,
        note=payload.note,
    )
    db.add(relationship)
    db.commit()
    db.refresh(relationship)
    return relationship


@router.get("/search/hybrid", response_model=LibrarySearchResponseOut)
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
async def search_library(
    request: Request,
    q: str,
    top_k: int = 10,
    project_id: uuid.UUID | None = None,
    classification: str | None = None,
    active_truth_status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    query = LibrarySearchQuery(
        query=q, top_k=top_k, project_id=project_id, classification=classification, active_truth_status=active_truth_status
    )

    # Real local fallback (not a generic 500, not a silently empty result): if the
    # embedding provider is unreachable/unconfigured, `vector` stays None and
    # hybrid_search() below simply skips its semantic channel — the independent
    # text-match (ILIKE) channel still runs and returns real results. The response
    # tells the client exactly what happened via semantic_search_available/degraded_reason
    # rather than pretending semantic search ran and just found nothing.
    vector: list[float] | None = None
    degraded_reason: str | None = None
    try:
        provider, model = resolve_active(db, role="embedding")
        vectors = await provider.embed([query.query], model=model)
        vector = vectors[0]
    except Exception as exc:  # noqa: BLE001 - every failure mode must degrade, never 500
        degraded_reason = classify_provider_exception(exc).message

    results = hybrid_search(
        db,
        user.id,
        vector,
        query.query,
        top_k=query.top_k,
        project_id=query.project_id,
        classification=query.classification,
        active_truth_status=query.active_truth_status,
    )
    return LibrarySearchResponseOut(
        results=results,
        semantic_search_available=vector is not None,
        degraded_reason=degraded_reason,
    )
