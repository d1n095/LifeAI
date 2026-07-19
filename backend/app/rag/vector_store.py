import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.document_chunk import DocumentChunk


def upsert_chunks(
    db: Session,
    document_id: uuid.UUID,
    owner_id: uuid.UUID,
    chunks: list[str],
    vectors: list[list[float]],
) -> int:
    """Writes chunk rows scoped to owner_id (see app/models/document_chunk.py and the
    document_chunks_isolation policy in app/rls.py). The caller MUST have already run
    `SET LOCAL app.current_user_id` for this session, set to this same owner_id — RLS's
    WITH CHECK rejects the insert otherwise regardless of what's passed here. That's
    deliberate: owner_id being "correct" at the Python level is not what enforces
    isolation, the database is — this parameter is defense in depth, not the boundary
    itself. See app/rag/ingest.py for why the background indexing task has to set that
    session variable explicitly instead of relying on the usual request-scoped mechanism
    in app/deps.py / app/db.py.
    """
    rows = [
        DocumentChunk(
            document_id=document_id,
            owner_id=owner_id,
            chunk_index=idx,
            text=chunk,
            embedding=vector,
        )
        for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    db.add_all(rows)
    db.flush()
    return len(rows)


def search(db: Session, owner_id: uuid.UUID, vector: list[float], top_k: int = 5) -> list[dict]:
    """Explicit owner_id filter in addition to RLS — see upsert_chunks's docstring on why
    this isn't relying on a single layer.

    pgvector's `<=>` operator (exposed here via .cosine_distance()) returns cosine
    DISTANCE (0 = identical direction, 2 = opposite) — the inverse of what Qdrant's
    Distance.COSINE search previously returned (a similarity score, higher = better, which
    is what app/rag/trust.py's assess_confidence() thresholds are calibrated against).
    `1 - distance` converts back to that same similarity scale — getting this backwards
    would silently invert the Trust Engine's confidence judgement (a strong match would
    look weak and vice versa) without raising any error, so this conversion is deliberate
    and not just a cosmetic rename.
    """
    distance = DocumentChunk.embedding.cosine_distance(vector)
    stmt = (
        select(DocumentChunk, distance.label("distance"))
        .where(DocumentChunk.owner_id == owner_id)
        .order_by(distance)
        .limit(top_k)
    )
    rows = db.execute(stmt).all()
    return [
        {
            "document_id": str(chunk.document_id),
            "title": chunk.document.title if chunk.document else None,
            "text": chunk.text,
            "score": 1 - dist,
        }
        for chunk, dist in rows
    ]


def delete_document_chunks(db: Session, document_id: uuid.UUID) -> None:
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
