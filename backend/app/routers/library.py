import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, field_validator
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.models.document import ActiveTruthStatus, Document, KnowledgeClassification
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.source_relationship import SourceRelationship
from app.models.user import User
from app.providers.registry import resolve_active
from app.rag.library_import import run_import_job
from app.rag.trust import assess_claim_confidence
from app.rag.vector_store import hybrid_search
from app.rag.zip_import import sha256_bytes
from app.schemas import (
    DeleteConfirmIn,
    ImportJobOut,
    KnowledgeClaimOut,
    KnowledgeSourceDetailOut,
    KnowledgeSourceOut,
    LibrarySearchHit,
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
    background_tasks: BackgroundTasks,
    file: UploadFile,
    project_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Filen är för stor (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    if not raw:
        raise HTTPException(status_code=400, detail="Filen är tom.")
    if project_id is not None:
        from app.models.project import Project

        if db.query(Project).filter_by(id=project_id).first() is None:
            raise HTTPException(status_code=404, detail="Projektet hittades inte.")

    checksum = sha256_bytes(raw)

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
        return existing

    job = ImportJob(
        owner_id=user.id,
        project_id=project_id,
        status=ImportJobStatus.pending,
        source_filename=file.filename,
        source_checksum=checksum,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(_run_import_job_background, job.id, user.id, raw, file.filename or "upload", project_id)
    return job


def _run_import_job_background(job_id: uuid.UUID, owner_id: uuid.UUID, raw: bytes, filename: str, project_id: uuid.UUID | None) -> None:
    import asyncio

    from app.db import SessionLocal

    async def run():
        db = SessionLocal()
        try:
            await run_import_job(db, job_id, owner_id, raw, filename, project_id=project_id)
        finally:
            db.close()

    asyncio.run(run())


@router.get("/jobs/{job_id}", response_model=ImportJobOut)
def get_import_job(job_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    job = db.query(ImportJob).filter_by(id=job_id, owner_id=user.id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Importjobbet hittades inte.")
    return job


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
            status=c.status.value,
            confidence=assess_claim_confidence(db, c).value,
            grounding_score=c.grounding_score,
            chunk_id=c.chunk_id,
            created_at=c.created_at,
        )
        for c in claims
    ]
    return detail


@router.delete("/{source_id}")
def delete_source(
    source_id: uuid.UUID,
    payload: DeleteConfirmIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Soft delete (Document.deleted_at) + immediate hard purge of the chunks/embeddings
    that make the source searchable at all — see app/rag/vector_store.py's search()/
    hybrid_search(), which both exclude deleted_at IS NOT NULL sources, and this purge,
    which means even a direct DocumentChunk query (bypassing those functions) would find
    nothing. KnowledgeVersion/SourceRelationship rows are kept as revision history — see
    docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's "Export och radering" section for why."""
    document = _visible_document_query(db, user.id).filter(Document.id == source_id).first()
    if document is None:
        raise HTTPException(status_code=404, detail="Källan hittades inte.")
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Radering kräver explicit bekräftelse (confirm: true).")

    db.query(DocumentChunk).filter_by(document_id=source_id).delete(synchronize_session=False)
    document.deleted_at = datetime.utcnow()
    document.chunk_count = 0
    db.add(document)
    db.commit()
    return {"status": "deleted"}


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


@router.get("/search/hybrid", response_model=list[LibrarySearchHit])
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
    provider, model = resolve_active(db, role="embedding")
    vectors = await provider.embed([query.query], model=model)
    return hybrid_search(
        db,
        user.id,
        vectors[0],
        query.query,
        top_k=query.top_k,
        project_id=query.project_id,
        classification=query.classification,
        active_truth_status=query.active_truth_status,
    )
