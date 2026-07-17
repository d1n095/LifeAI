import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.document import Document, DocumentSource
from app.rag.extract import extract_text
from app.rag.ingest import index_document
from app.rag.qdrant_store import delete_document as delete_from_qdrant
from app.schemas import DocumentOut

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.get("", response_model=list[DocumentOut])
def list_documents(db: Session = Depends(get_db)):
    return db.query(Document).order_by(Document.created_at.desc()).all()


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    raw = await file.read()
    text = extract_text(file.filename, raw)

    document = Document(
        title=file.filename,
        source=DocumentSource.upload,
        category=category,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

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
def delete_document(document_id: uuid.UUID, db: Session = Depends(get_db)):
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Dokumentet hittades inte.")
    delete_from_qdrant(document_id)
    db.delete(document)
    db.commit()
    return {"status": "deleted"}
