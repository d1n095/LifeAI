import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.document import Document, IndexStatus
from app.providers.registry import resolve_active
from app.rag.chunking import chunk_text
from app.rag.vector_store import upsert_chunks


async def index_document(db: Session, document: Document, text_content: str) -> None:
    """Chunk, embed and store a document's text as pgvector-backed DocumentChunk rows.
    Updates status on the Document row.

    document.uploaded_by becomes the chunks' owner_id (see app/models/document_chunk.py) —
    required, not optional: a document with no uploader has nothing to scope chunk RLS to,
    so indexing is refused rather than silently writing ownerless (and therefore
    RLS-unreachable-by-anyone) rows. In practice this never happens today — the only wired
    ingestion path (POST /api/documents/upload, app/routers/documents.py) always sets it —
    but this is guarded explicitly rather than assumed, since a future ingestion path
    (e.g. the website crawler in docs/ROADMAP.md's Fas 2) could easily forget to.

    This session's `app.current_user_id` must already be set to document.uploaded_by before
    this runs (see app/rls.py's document_chunks_isolation policy) — the caller here
    (app/routers/documents.py's background task) does that explicitly, because a background
    task's fresh SessionLocal() never goes through app/deps.py's get_current_user, which is
    normally what sets it for a real request.
    """
    if document.uploaded_by is None:
        document.status = IndexStatus.failed
        document.error_message = "Dokumentet saknar ägare — kan inte indexeras."
        db.add(document)
        db.commit()
        return

    # Life Library upload consolidation: `embedding` (not the legacy `indexing`) is the
    # granular status for "chunking/embedding is in progress" — see IndexStatus's docstring.
    # The document row itself (and its extracted text, held in `text_content` by the caller)
    # already exists before this point, so a failure below never loses the received material.
    document.status = IndexStatus.embedding
    db.add(document)
    db.commit()

    try:
        chunks = chunk_text(text_content)
        if not chunks:
            document.status = IndexStatus.failed
            document.error_message = "Inget textinnehåll kunde extraheras."
            db.add(document)
            db.commit()
            return

        provider, model = resolve_active(db, role="embedding")
        vectors = await provider.embed(chunks, model=model)
        count = upsert_chunks(db, document.id, document.uploaded_by, chunks, vectors)

        document.status = IndexStatus.indexed
        document.chunk_count = count
        document.content_preview = text_content[:1000]
        document.error_message = None
    except Exception as exc:  # noqa: BLE001 - surface any ingestion failure on the document row
        document.status = IndexStatus.failed
        document.error_message = str(exc)
    finally:
        db.add(document)
        db.commit()


async def reindex_document_id(db: Session, document_id: uuid.UUID, text_content: str) -> None:
    document = db.get(Document, document_id)
    if document is None:
        raise ValueError("Dokumentet finns inte.")
    if document.uploaded_by is not None:
        db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(document.uploaded_by)})
    await index_document(db, document, text_content)
