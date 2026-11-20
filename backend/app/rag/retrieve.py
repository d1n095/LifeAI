import uuid

from sqlalchemy.orm import Session

from app.providers.base import ProviderError
from app.providers.registry import embed_with_policy, resolve_active
from app.rag.embedding_space import native_dim_of
from app.rag.query_embedding import prepare_embedding_query
from app.rag.vector_store import search


class GroundingUnavailableError(ProviderError):
    """Retrieval cannot run under the effect guard with the active embedding provider.

    This is deliberately distinct from a generic provider outage: nothing left the process,
    and the remedy is configuration (local Ollama embedding, or a metered embedding
    contract), not a retry against another provider. Chat surfaces it as an explicit
    degraded-grounding reason instead of pretending the answer was grounded.
    """


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
    try:
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
    except ProviderError as exc:
        # The guard refused the active provider before any outbound call. Make the cause
        # explicit and actionable; do not let it collapse into a generic "provider down".
        if effect_guard and exc.category == "unsupported" and provider.name != "ollama":
            raise GroundingUnavailableError(
                "Kunskapssökning är inte tillgänglig: den aktiva embedding-leverantören "
                "är extern och saknar ett mätbart effektavtal. Konfigurera lokal "
                "Ollama-embedding (roll: embedding) eller en embedding-leverantör med "
                "mätbart effektavtal.",
                category="unsupported",
                provider_request_may_have_left=False,
            ) from None
        raise
    # Scope retrieval to the ACTIVE embedding space (model + this query's native dim) so a
    # query embedded with model X can only ever match chunks embedded with model X — no
    # silent cross-model mixing (app/rag/embedding_space.py). search() zero-pads the query to
    # the storage width itself.
    native_dim = native_dim_of(vectors)
    return search(
        db,
        owner_id,
        vectors[0],
        top_k=top_k,
        project_id=project_id,
        document_id=document_id,
        embedding_model=model,
        embedding_native_dim=native_dim,
    )
