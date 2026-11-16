import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.jobs.heartbeat import worker_process_alive
from app.library.import_uploads import EmptyUploadError, receive_import_upload
from app.limiter import limiter
from app.models.document import ActiveTruthStatus, Document, KnowledgeClassification
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.media_url_import import MediaUrlImport
from app.models.source_relationship import SourceRelationship
from app.models.user import User
from app.egress_policy import EgressDeniedError
from app.library.originals import (
    LoadedOriginal,
    OriginalIntegrityError,
    OriginalNotAvailable,
    OriginalNotFound,
    load_original,
)
from app.providers.registry import embed_with_policy, resolve_active
from app.providers.verification import classify_provider_exception
from app.rag.embedding_space import native_dim_of
from app.rag.trust import assess_claim_confidence
from app.rag.vector_store import hybrid_search
from app.storage import get_storage
from app.storage.base import StorageError, StorageSizeLimitExceeded
from app.storage.purge import SourcePurgeNotFoundError, purge_source
from app.storage.references import (
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
logger = logging.getLogger("mainai.library")

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
def import_package(
    request: Request,
    file: UploadFile,
    project_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        return _import_package_impl(file, project_id, db, user)
    except OperationalError as exc:
        try:
            db.rollback()
        except OperationalError:
            logger.warning("Could not roll back the failed library-import transaction.")
        logger.warning(
            "Database unavailable during library import (%s); returning a retryable response.",
            type(exc.orig).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Tjänsten är tillfälligt otillgänglig. Försök igen med samma fil.",
            headers={"Retry-After": "3"},
        ) from exc


def _import_package_impl(
    file: UploadFile,
    project_id: uuid.UUID | None,
    db: Session,
    user: User,
):
    if project_id is not None:
        from app.models.project import Project

        if db.query(Project).filter_by(id=project_id).first() is None:
            raise HTTPException(status_code=404, detail="Projektet hittades inte.")

    try:
        return receive_import_upload(
            db,
            owner_id=user.id,
            source_filename=file.filename,
            media_type=file.content_type,
            read_chunk=lambda: file.file.read(1 << 20),
            max_bytes=MAX_UPLOAD_BYTES,
            project_id=project_id,
            purge_empty_blob=True,
        ).job
    except StorageSizeLimitExceeded:
        raise HTTPException(status_code=413, detail=f"Filen är för stor (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    except EmptyUploadError:
        raise HTTPException(status_code=400, detail="Filen är tom.")
    except OSError as exc:
        logger.warning("Staged upload storage is temporarily unavailable.")
        raise HTTPException(
            status_code=503,
            detail="Lagringen är tillfälligt otillgänglig. Försök igen med samma fil.",
            headers={"Retry-After": "3"},
        ) from exc
    except StorageError as exc:
        logger.exception("Could not persist library-import upload.")
        raise HTTPException(status_code=500, detail="Kunde inte lagra filen säkert.") from exc


RECENT_JOBS_LIMIT = 50


@router.get("/jobs", response_model=list[ImportJobOut])
def list_import_jobs(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Life Library upload consolidation package (DEL 3): lets the frontend's upload queue
    recover real server job state after a page reload or a fresh login — receiving,
    pending/running (still in-flight), and recently finished jobs are returned, newest first,
    so the client never has to guess or show 'completed' before the server actually says so."""
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


def _document_is_media_source(document: Document) -> bool:
    """True when this document is an audio/video source the /media endpoint may serve.
    Text/document imports also get storage_key — that alone does not make them media."""
    if document.media_duration_seconds is not None or document.media_blob is not None:
        return True
    media_type = document.media_type or ""
    return media_type.startswith("audio/") or media_type.startswith("video/")


def _read_document_media_bytes(document: Document) -> bytes | None:
    """Return playable media bytes for a document. Prefer content-addressed durable storage
    (storage_key) — the canonical original — over legacy in-DB media_blob duplicates."""
    if document.storage_key:
        storage = get_storage()
        try:
            with storage.open_read(document.storage_key) as handle:
                return handle.read()
        except FileNotFoundError:
            pass
        except StorageError:
            return None
    if document.media_blob is not None:
        return document.media_blob
    return None


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
    """STEG 13: streams the raw bytes of an audio/video import back to an <audio>/<video>
    element. Reads from the content-addressed original at Document.storage_key (see
    app/storage/local_fs.py); falls back to legacy Document.media_blob only for rows
    imported before that path existed. Same RLS-scoped, deleted_at-excluding query every
    other source-detail route uses — a deleted or someone-else's source 404s here exactly
    like it does everywhere else in this router, not just a "hidden from listings" soft
    block."""
    document = _visible_document_query(db, user.id).filter(Document.id == source_id).first()
    if document is None or not _document_is_media_source(document):
        raise HTTPException(status_code=404, detail="Ingen mediefil hittades för den här källan.")

    media_bytes = _read_document_media_bytes(document)
    if media_bytes is None:
        raise HTTPException(status_code=404, detail="Ingen mediefil hittades för den här källan.")
    return Response(content=media_bytes, media_type=document.media_type or "application/octet-stream")


def _content_disposition(filename: str) -> str:
    """RFC 6266 / RFC 5987 `attachment` disposition. Always `attachment` (never `inline`) so a
    browser downloads the original rather than rendering a text/html or image/svg+xml original
    in the founder's session. `filename` is already sanitized (app/library/originals.py
    safe_filename, ASCII `[A-Za-z0-9._ -]` only), so the quoted fallback is header-safe; the
    `filename*` UTF-8 form is added for completeness."""
    from urllib.parse import quote

    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"


@router.get(
    "/{source_id}/original",
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": "Den oförändrade originalfilens råa bytes (nedladdning).",
        }
    },
)
def get_source_original(
    source_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Blueprint §16.6: streams the untouched, content-addressed ORIGINAL of any source kind
    back to the founder — the non-media counterpart to `GET /{source_id}/media`, so a founder
    can download exactly the bytes they uploaded (text/PDF/DOCX/...), not just a derived
    preview. All authorization, owner-scoping, integrity verification and auditing live in the
    shared `load_original` service (app/library/originals.py), used by Life's own authorized
    path too; this handler only maps typed failures to HTTP status and sets safe headers.

    Bytes are always served as `application/octet-stream` with an `attachment` disposition
    (never the declared media type) so an original that happens to be HTML/SVG can never render
    inline in the founder's authenticated session. The content-addressed storage key is never
    exposed. No AI/provider call and no egress happen here — reading an original is not
    authority to disclose it externally."""
    try:
        original: LoadedOriginal = load_original(
            db,
            owner_id=user.id,
            source_id=source_id,
            actor_type="founder",
            actor_id=user.id,
            access_label="founder_download",
            request=request,
        )
    except (OriginalNotFound, OriginalNotAvailable):
        # Deliberately one 404 for both: never reveal whether a not-visible source exists.
        raise HTTPException(status_code=404, detail="Originalet hittades inte.")
    except OriginalIntegrityError:
        raise HTTPException(status_code=503, detail="Originalet kunde inte verifieras just nu.")

    return Response(
        content=original.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(original.filename),
            # Belt-and-suspenders against content sniffing turning octet-stream into HTML.
            "X-Content-Type-Options": "nosniff",
        },
    )


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
    active_model: str | None = None
    active_native_dim: int | None = None
    degraded_reason: str | None = None
    try:
        provider, model = resolve_active(db, role="embedding")
        vectors = await embed_with_policy(
            db,
            provider,
            [query.query],
            model=model,
            owner_id=user.id,
            purpose="rag_query",
            requested_by="routers.library.search_library",
        )
        vector = vectors[0]
        # Carry the active embedding space so the semantic channel only ranks chunks embedded
        # with this same model/dim (app/rag/embedding_space.py); the text channel is unaffected.
        active_model = model
        active_native_dim = native_dim_of(vectors)
    except EgressDeniedError as exc:
        # Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V2 query-
        # embedding half): a policy refusal, not a provider outage -- str(exc) is safe to show
        # (fixed, static description, never raw query content). Same graceful degrade as any
        # other failure mode here: vector stays None, the independent ILIKE text-match channel
        # still runs, never a silent retry against a different provider.
        degraded_reason = str(exc)
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
        embedding_model=active_model,
        embedding_native_dim=active_native_dim,
    )
    return LibrarySearchResponseOut(
        results=results,
        semantic_search_available=vector is not None,
        degraded_reason=degraded_reason,
    )
