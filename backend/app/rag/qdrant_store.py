import uuid
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.config import get_settings


@lru_cache
def get_qdrant_client() -> QdrantClient:
    settings = get_settings()
    return QdrantClient(url=settings.qdrant_url)


def ensure_collection(dim: int | None = None) -> None:
    settings = get_settings()
    client = get_qdrant_client()
    dim = dim or settings.embedding_dim
    existing = [c.name for c in client.get_collections().collections]
    if settings.qdrant_collection not in existing:
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=qmodels.VectorParams(size=dim, distance=qmodels.Distance.COSINE),
        )


def upsert_chunks(document_id: uuid.UUID, title: str, category: str | None, chunks: list[str], vectors: list[list[float]]) -> int:
    settings = get_settings()
    client = get_qdrant_client()
    points = [
        qmodels.PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={
                "document_id": str(document_id),
                "title": title,
                "category": category,
                "chunk_index": idx,
                "text": chunk,
            },
        )
        for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    if points:
        client.upsert(collection_name=settings.qdrant_collection, points=points)
    return len(points)


def search(vector: list[float], top_k: int = 5) -> list[dict]:
    settings = get_settings()
    client = get_qdrant_client()
    results = client.search(collection_name=settings.qdrant_collection, query_vector=vector, limit=top_k)
    return [
        {
            "document_id": hit.payload.get("document_id"),
            "title": hit.payload.get("title"),
            "text": hit.payload.get("text"),
            "score": hit.score,
        }
        for hit in results
    ]


def delete_document(document_id: uuid.UUID) -> None:
    settings = get_settings()
    client = get_qdrant_client()
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=qmodels.FilterSelector(
            filter=qmodels.Filter(
                must=[qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=str(document_id)))]
            )
        ),
    )
