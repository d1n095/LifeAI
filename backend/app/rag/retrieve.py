import uuid

from sqlalchemy.orm import Session

from app.providers.registry import resolve_active
from app.rag.vector_store import search


async def retrieve_context(
    db: Session,
    owner_id: uuid.UUID,
    query: str,
    top_k: int = 5,
    *,
    project_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
) -> list[dict]:
    provider, model = resolve_active(db, role="embedding")
    vectors = await provider.embed([query], model=model)
    return search(db, owner_id, vectors[0], top_k=top_k, project_id=project_id, document_id=document_id)
