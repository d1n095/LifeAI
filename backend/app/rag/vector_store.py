import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.document import Document
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


def _hit_dict(chunk: DocumentChunk, score: float) -> dict:
    doc = chunk.document
    return {
        "document_id": str(chunk.document_id),
        "chunk_id": str(chunk.id),
        "title": doc.title if doc else None,
        "text": chunk.text,
        "score": score,
        "classification": doc.classification.value if doc else None,
        "active_truth_status": doc.active_truth_status.value if doc else None,
        "media_type": doc.media_type if doc else None,
        "project_id": str(doc.project_id) if doc and doc.project_id else None,
        # STEG 12: set only for a chunk built from a timed transcript segment
        # (app/rag/media_import.py) — None for ordinary text chunks, exactly mirroring
        # DocumentChunk.start_seconds/end_seconds themselves. Lets a citation (chat.py's
        # SourceRef, library search hits) open the source at the exact moment instead of
        # just the source itself.
        "start_seconds": chunk.start_seconds,
        "end_seconds": chunk.end_seconds,
    }


def search(
    db: Session,
    owner_id: uuid.UUID,
    vector: list[float],
    top_k: int = 5,
    *,
    project_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
) -> list[dict]:
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

    Joins Document explicitly (not just via the viewonly relationship used for display) so
    a soft-deleted source (Document.deleted_at set — see migration 0006 and
    app/routers/library.py's delete_source) can never surface here: "raderat material blir
    osökbart direkt" is a Founder Knowledge Studio safety requirement, not a UI nicety — this
    is also what app/rag/retrieve.py's chat-context retrieval calls, so a deleted source can
    never be quoted back to the founder as if it still existed either.
    """
    distance = DocumentChunk.embedding.cosine_distance(vector)
    stmt = (
        select(DocumentChunk, distance.label("distance"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.owner_id == owner_id, Document.deleted_at.is_(None))
    )
    if project_id is not None:
        stmt = stmt.where(Document.project_id == project_id)
    if document_id is not None:
        stmt = stmt.where(Document.id == document_id)
    stmt = stmt.order_by(distance).limit(top_k)
    rows = db.execute(stmt).all()
    return [_hit_dict(chunk, 1 - dist) for chunk, dist in rows]


def hybrid_search(
    db: Session,
    owner_id: uuid.UUID,
    vector: list[float] | None,
    query_text: str,
    top_k: int = 10,
    *,
    project_id: uuid.UUID | None = None,
    classification: str | None = None,
    active_truth_status: str | None = None,
) -> list[dict]:
    """DEL 5's hybrid search: combines the same semantic (pgvector) channel `search()` uses
    with a plain case-insensitive substring match on chunk text/document title, then merges
    and re-ranks by chunk id so a chunk found by both channels doesn't appear twice and gets
    credit for matching both ways. A pure-vector search can miss an exact term a founder
    typed verbatim (e.g. a proper noun or code identifier the embedding model doesn't weight
    heavily); a pure-text search misses paraphrases — this is why both run, not either
    alone. Filters (project/classification/active_truth_status) apply to both channels
    identically, and deleted sources are excluded the same way search() excludes them.

    `vector=None` skips the semantic channel entirely (real local fallback when no embedding
    provider is available — see app/routers/library.py's search_library) rather than faking a
    zero vector, which would return meaningless cosine-distance "matches". The text-match
    channel needs no embedding at all, so it keeps returning real results either way.
    """
    base_filters = [DocumentChunk.owner_id == owner_id, Document.deleted_at.is_(None)]
    if project_id is not None:
        base_filters.append(Document.project_id == project_id)
    if classification is not None:
        base_filters.append(Document.classification == classification)
    if active_truth_status is not None:
        base_filters.append(Document.active_truth_status == active_truth_status)

    semantic_rows = []
    if vector is not None:
        distance = DocumentChunk.embedding.cosine_distance(vector)
        semantic_stmt = (
            select(DocumentChunk, distance.label("distance"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*base_filters)
            .order_by(distance)
            .limit(top_k)
        )
        semantic_rows = db.execute(semantic_stmt).all()

    like_pattern = f"%{query_text}%"
    text_stmt = (
        select(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*base_filters, or_(DocumentChunk.text.ilike(like_pattern), Document.title.ilike(like_pattern)))
        .limit(top_k)
    )
    text_rows = db.execute(text_stmt).scalars().all()

    merged: dict[uuid.UUID, dict] = {}
    for chunk, dist in semantic_rows:
        merged[chunk.id] = _hit_dict(chunk, 1 - dist)
    TEXT_MATCH_BOOST = 0.05
    for chunk in text_rows:
        if chunk.id in merged:
            merged[chunk.id]["score"] = min(1.0, merged[chunk.id]["score"] + TEXT_MATCH_BOOST)
            merged[chunk.id]["text_match"] = True
        else:
            hit = _hit_dict(chunk, TEXT_MATCH_BOOST)
            hit["text_match"] = True
            merged[chunk.id] = hit

    for hit in merged.values():
        hit.setdefault("text_match", False)

    results = sorted(merged.values(), key=lambda h: h["score"], reverse=True)
    return results[:top_k]


def delete_document_chunks(db: Session, document_id: uuid.UUID) -> None:
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
