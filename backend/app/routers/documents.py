import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import get_current_user
from app.models.document import Document, DocumentSource
from app.models.user import User
from app.rag.extract import extract_text
from app.rag.ingest import index_document
from app.rag.qdrant_store import delete_document as delete_from_qdrant
from app.schemas import DocumentOut

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(get_current_user)])


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
    user: User = Depends(get_current_user),
):
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Filen är för stor (max 25 MB).")

    text = extract_text(file.filename, raw)

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

    background_tasks.add_task(_index_in_background, document.id, text)
    return document


def _index_in_background(document_id: uuid.UUID, text: str) -> None:
    import asyncio

    from app.db import SessionLocal

    async def run():
        db = SessionLocal()
        try:
            document = db.get(Document, document_id)
            if document:
                await index_document(db, document, text)
        finally:
            db.close()

    asyncio.run(run())


@router.delete("/{document_id}")
def delete_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Dokumentet hittades inte.")
    delete_from_qdrant(document_id)
    db.delete(document)
    db.commit()
    record_audit(db, user_id=user.id, action="document_delete", entity_type="document", entity_id=str(document_id), request=request)
    return {"status": "deleted"}
