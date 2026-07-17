from sqlalchemy.orm import Session

from app.providers.registry import resolve_active
from app.rag.qdrant_store import search


async def retrieve_context(db: Session, query: str, top_k: int = 5) -> list[dict]:
    provider, model = resolve_active(db, role="embedding")
    vectors = await provider.embed([query], model=model)
    return search(vectors[0], top_k=top_k)
