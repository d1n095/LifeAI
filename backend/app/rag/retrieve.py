import uuid

from sqlalchemy.orm import Session

from app.providers.registry import embed_with_policy, resolve_active
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
    # Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V2 query-
    # embedding half): owner_id is already this function's own required parameter -- no
    # identity-propagation gap to solve here, unlike some chat_with_fallback() callers. Raises
    # EgressDeniedError (never caught here) on a NEVER_EGRESS-marked query; every caller of
    # this function is responsible for its own graceful-degradation behavior on that, exactly
    # as each already is for ProviderError/httpx.HTTPError -- see app/routers/chat.py's
    # _attempt_assistant_reply() for the one caller that currently degrades gracefully rather
    # than 500ing.
    provider, model = resolve_active(db, role="embedding")
    vectors = await embed_with_policy(
        db,
        provider,
        [query],
        model=model,
        owner_id=owner_id,
        purpose="rag_retrieval",
        requested_by="rag.retrieve.retrieve_context",
    )
    return search(db, owner_id, vectors[0], top_k=top_k, project_id=project_id, document_id=document_id)
