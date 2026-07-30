import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import require_founder
from app.models.document import Document, DocumentSource
from app.models.user import User
from app.rag.extract import extract_text
from app.rag.ingest import index_document
from app.rag.source_purge import SourcePurgeNotFoundError, purge_source
from app.schemas import DocumentOut

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(require_founder)])


@router.get("", response_model=list[DocumentOut])
def list_documents(db: Session = Depends(get_db)):
    # Document now has a soft-delete flag (deleted_at — see migration 0006 and
    # app/routers/library.py's delete_source): a source removed through the newer Founder
    # Knowledge Studio Library UI must not keep reappearing here, in the older /documents
    # view of the same underlying table. Found as a real bug (not assumed) — an E2E test
    # deleting a source via /library still saw it listed via /documents.
    return db.query(Document).filter(Document.deleted_at.is_(None)).order_by(Document.created_at.desc()).all()


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile,
    category: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Filen är för stor (max 25 MB).")

    text_content = extract_text(file.filename, raw)

    document = Document(
        title=file.filename,
        source=DocumentSource.upload,
        category=category,
        uploaded_by=user.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    record_audit(db, user_id=user.id, action="document_upload", entity_type="document", entity_id=str(document.id), request=request)

    background_tasks.add_task(_index_in_background, document.id, text_content)
    return document


def _index_in_background(document_id: uuid.UUID, text_content: str) -> None:
    import asyncio

    from app.db import SessionLocal

    async def run():
        db = SessionLocal()
        try:
            document = db.get(Document, document_id)
            if document is None or document.uploaded_by is None:
                return
            # A background task's fresh SessionLocal() never goes through
            # app/deps.py's get_current_user — nothing else sets app.current_user_id for
            # this session, so document_chunks_isolation (app/rls.py) would reject every
            # write here (NULL current_user_id matches nothing) without this explicit call.
            # See app/rag/ingest.py's index_document docstring.
            db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(document.uploaded_by)})
            await index_document(db, document, text_content)
        finally:
            db.close()

    asyncio.run(run())


@router.delete("/{document_id}")
def delete_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Thin wrapper delegating to the shared app/rag/source_purge.py::purge_source() service
    — see that module's docstring. INTENTIONAL behavior change from the old hard `db.delete
    (document)` (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8's "En gemensam purge-tjänst"):
    migration 0019's `document_source_units.document_id` FK (no `ON DELETE` action) would
    otherwise RESTRICT-block a hard delete outright once any memory_source_units row exists
    for this document, and the old implementation's own previous docstring already flagged an
    unresolved multi-uploader chunk-deletion gap besides (the old code's `db.get(Document,
    document_id)` never even checked `uploaded_by`). This route now gets the same soft-delete
    + blob-purge + memory-purge behavior `/api/library` already had, including
    `purge_source()`'s own explicit ownership check (defense in depth alongside `documents`'
    RLS policy, not a replacement for it — see app/rls.py) — not a second, diverging
    implementation."""
    try:
        result = purge_source(db, document_id, user.id)
    except SourcePurgeNotFoundError:
        raise HTTPException(status_code=404, detail="Dokumentet hittades inte.")

    record_audit(
        db,
        user_id=user.id,
        action="source_purged",
        entity_type="document",
        entity_id=str(document_id),
        detail=(
            f"sources_purged={result.sources_purged} sources_already_purged={result.sources_already_purged} "
            f"chunks_deleted={result.chunks_deleted} claims_preserved={result.claims_preserved} "
            f"legacy_without_memory_source={result.legacy_without_memory_source} deletion_status={result.deletion_status.value}"
        ),
        request=request,
    )
    return {"status": "deleted"}
