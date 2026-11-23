import uuid

from sqlalchemy.orm import Session

from app.providers.registry import embed_with_policy, resolve_active
from app.rag.embedding_space import native_dim_of
from app.rag.query_embedding import prepare_embedding_query
from app.rag.ranking import rank_hits, superseded_document_ids
from app.rag.vector_store import search


async def retrieve_context(
    db: Session,
    owner_id: uuid.UUID,
    query: str,
    top_k: int = 5,
    *,
    project_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    effect_guard: bool = False,
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
        # Instruction-aware models need the prefix on the QUERY only; documents stay bare
        # (app/rag/query_embedding.py). A no-op for symmetric models and the empty default.
        [prepare_embedding_query(query)],
        model=model,
        owner_id=owner_id,
        purpose="rag_retrieval",
        requested_by="rag.retrieve.retrieve_context",
        effect_guard=effect_guard,
    )
    # Scope retrieval to the ACTIVE embedding space (model + this query's native dim) so a
    # query embedded with model X can only ever match chunks embedded with model X — no
    # silent cross-model mixing (app/rag/embedding_space.py). search() zero-pads the query to
    # the storage width itself.
    native_dim = native_dim_of(vectors)
    # Over-fetch before re-ranking. Status/version authority can only reorder documents that
    # are actually candidates; fetching exactly top_k would let a superseded draft crowd the
    # final report out of the candidate set before ranking ever sees it. The extra rows cost
    # one query and are discarded after ranking (nothing is suppressed — see ranking.py).
    candidate_k = max(top_k * 4, 20)
    hits = search(
        db,
        owner_id,
        vectors[0],
        top_k=candidate_k,
        project_id=project_id,
        document_id=document_id,
        embedding_model=model,
        embedding_native_dim=native_dim,
    )
    if not hits:
        return []
    return rank_hits(
        hits,
        superseded_ids=superseded_document_ids(db, owner_id, [h["document_id"] for h in hits]),
        top_k=top_k,
    )
