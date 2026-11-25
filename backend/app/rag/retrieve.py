import uuid

import httpx
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


class RetrievalUnavailableError(GroundingUnavailableError):
    """The embedding transport failed, or returned no usable vector.

    Unlike ``GroundingUnavailableError``'s pre-egress refusal, this failure may have reached
    the provider, so ``provider_request_may_have_left`` is preserved instead of asserted
    False. It deliberately subclasses ``GroundingUnavailableError`` so every existing
    retrieval caller (chat, knowledge, workbench, blueprint) already maps it to an explicit
    degraded/503 response -- never an unhandled 500 and never silently empty grounding.
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
    from app.egress_policy import EgressDeniedError

    try:
        provider, model = resolve_active(db, role="embedding")
    except ProviderError as exc:
        # No embedding provider could be activated. This is a configuration failure before
        # any outbound call, not an uncertain provider effect.
        raise RetrievalUnavailableError(
            "Kunskapssökning är inte tillgänglig: ingen embedding-leverantör kunde aktiveras.",
            category=exc.category or "unreachable",
            provider_request_may_have_left=False,
        ) from None
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
    except EgressDeniedError:
        # Content denial is a policy decision, not a provider outage; callers map it to
        # their own explicit 422/degraded response and it must not be retried or masked.
        raise
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
        # A real provider failure (local Ollama down, invalid key, unsupported role, ...).
        # classify_provider_exception() produces a safe message; never surface a raw
        # httpx/provider exception that could contain a URL or credential.
        from app.providers.verification import classify_provider_exception

        outcome = classify_provider_exception(exc)
        raise RetrievalUnavailableError(
            f"Kunskapssökning är inte tillgänglig: {outcome.message}",
            category=exc.category or outcome.result.value,
            provider_request_may_have_left=exc.provider_request_may_have_left,
        ) from None
    except httpx.HTTPError as exc:
        # A configured-but-invalid key can raise an unwrapped httpx error from embed().
        # Preserve the explicit degraded contract instead of letting it become a 500.
        from app.providers.verification import classify_provider_exception

        outcome = classify_provider_exception(exc)
        raise RetrievalUnavailableError(
            f"Kunskapssökning är inte tillgänglig: {outcome.message}",
            category="unreachable",
            provider_request_may_have_left=True,
        ) from None

    if not vectors or not vectors[0]:
        # A provider that returns an empty batch/vector is a real failure, not "zero hits".
        # Without this guard vectors[0] raised an unhandled IndexError (HTTP 500) in every
        # caller, losing the explicit degraded-grounding contract entirely.
        raise RetrievalUnavailableError(
            "Kunskapssökning är inte tillgänglig: embedding-leverantören returnerade ingen vektor.",
            category="unreachable",
            provider_request_may_have_left=True,
        )
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
