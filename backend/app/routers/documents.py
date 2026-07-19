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
from app.rag.vector_store import delete_document_chunks
from app.schemas import DocumentOut

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(require_founder)])


@router.get("", response_model=list[DocumentOut])
def list_documents(db: Session = Depends(get_db)):
    return db.query(Document).order_by(Document.created_at.desc()).all()


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
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Dokumentet hittades inte.")
    # Deletes only the chunks THIS session's RLS context (the caller) owns — see
    # app/models/document_chunk.py. Documents themselves are shared/not RLS-protected (by
    # design, see app/rls.py), so a user deleting a document they didn't upload will not be
    # able to delete another uploader's chunks for it — those become orphaned (still
    # referencing a document_id that's about to stop existing), not silently deleted on
    # someone else's behalf. This is a real, currently-unresolved edge case worth a
    # deliberate decision (e.g. restricting document deletion to the uploader, or an
    # admin-only cleanup path) rather than a silent gap — flagged, not fixed here.
    delete_document_chunks(db, document_id)
    db.delete(document)
    db.commit()
    record_audit(db, user_id=user.id, action="document_delete", entity_type="document", entity_id=str(document_id), request=request)
    return {"status": "deleted"}
