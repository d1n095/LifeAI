import uuid

from sqlalchemy.orm import Session

from app.models.document import Document, IndexStatus
from app.providers.registry import resolve_active
from app.rag.chunking import chunk_text
from app.rag.qdrant_store import ensure_collection, upsert_chunks


async def index_document(db: Session, document: Document, text: str) -> None:
    """Chunk, embed and store a document's text in Qdrant. Updates status on the Document row."""
    document.status = IndexStatus.indexing
    db.add(document)
    db.commit()

    try:
        chunks = chunk_text(text)
        if not chunks:
            document.status = IndexStatus.failed
            document.error_message = "Inget textinnehåll kunde extraheras."
            db.add(document)
            db.commit()
            return

        provider, model = resolve_active(db, role="embedding")
        vectors = await provider.embed(chunks, model=model)
        ensure_collection(dim=len(vectors[0]))
        count = upsert_chunks(document.id, document.title, document.category, chunks, vectors)

        document.status = IndexStatus.indexed
        document.chunk_count = count
        document.content_preview = text[:1000]
        document.error_message = None
    except Exception as exc:  # noqa: BLE001 - surface any ingestion failure on the document row
        document.status = IndexStatus.failed
        document.error_message = str(exc)
    finally:
        db.add(document)
        db.commit()


async def reindex_document_id(db: Session, document_id: uuid.UUID, text: str) -> None:
    document = db.get(Document, document_id)
    if document is None:
        raise ValueError("Dokumentet finns inte.")
    await index_document(db, document, text)
