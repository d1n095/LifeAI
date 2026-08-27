import logging
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.provider_config import ProviderConfig
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ChatResult, LLMProvider, Message, ProviderError
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.openrouter_provider import OpenRouterProvider

logger = logging.getLogger(__name__)

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "deepseek": DeepSeekProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
}

# Reasonable default chat model per provider, used only when falling back to a provider that
# has no explicit admin-configured model (the primary provider's own model comes from
# ProviderConfig / settings, never from here).
DEFAULT_CHAT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.5-flash",
    "deepseek": "deepseek-chat",
    "openrouter": "openrouter/auto",
    "ollama": "llama3.1",
}


def provider_names() -> list[str]:
    return list(_PROVIDERS.keys())


def available_providers() -> list[LLMProvider]:
    return [cls() for cls in _PROVIDERS.values()]


def get_provider(name: str) -> LLMProvider:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ProviderError(f"Okänd leverantör: {name}")
    return cls()


def resolve_active(db: Session, role: str) -> tuple[LLMProvider, str]:
    """Resolve the active provider + model for a given role ("chat" | "embedding").

    Order of precedence: DB config (set via admin panel) -> settings defaults.
    This is the single place the rest of the platform depends on — swapping a
    provider never requires touching routers, RAG code or the frontend.
    """
    settings = get_settings()
    config = db.query(ProviderConfig).filter_by(role=role, is_active=True).first()
    if config:
        return get_provider(config.provider), config.model

    if role == "embedding":
        return get_provider(settings.default_embedding_provider), settings.default_embedding_model
    return get_provider(settings.default_llm_provider), settings.default_llm_model


async def embed_with_policy(
    db: Session,
    provider: LLMProvider,
    texts: list[str],
    *,
    model: str,
    owner_id: uuid.UUID | None = None,
    purpose: str | None = None,
    requested_by: str | None = None,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> list[list[float]]:
    """`provider.embed(texts, model=model)`, gated the same way `chat_with_fallback()` gates
    `.chat()` (Life Vault / External-AI Egress Control, V2 — see
    docs/LIFE_VAULT_EGRESS_CONTROL.md): when `owner_id` is passed, `texts` is routed through
    `enforce_egress_policy()` first -- default-deny, hard-deny on NEVER_EGRESS-marked content
    (the whole batch, never a partial embed with the poisoned chunk silently dropped), one
    disclosure-ledger row per call. `owner_id` defaults to None (gate skipped, unchanged
    legacy behavior) for the same incremental-adoption reason as `chat_with_fallback()`.

    Takes an already-resolved `provider`/`model` rather than resolving them itself: every
    existing call site already calls `resolve_active(db, role="embedding")` earlier in its own
    flow (for provider verification / pre-flight checks that must happen before this), and
    resolving a second time here would be redundant, not just extra work -- a provider config
    change landing between the two resolutions could then embed with a provider different from
    the one that was actually verified.
    """
    outbound_texts = texts
    if owner_id is not None:
        from app.egress_policy import enforce_egress_policy

        outbound_texts = enforce_egress_policy(
            db,
            owner_id=owner_id,
            provider=provider.name,
            model=model,
            purpose=purpose or "embedding",
            requested_by=requested_by or "providers.registry.embed_with_policy",
            payload=texts,
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
        )
    return await provider.embed(outbound_texts, model=model)


def resolve_chat_chain(db: Session) -> list[tuple[LLMProvider, str]]:
    """Primary chat provider first (DB config / settings default, as usual), then the
    remaining providers in CHAT_FALLBACK_ORDER that are actually configured (have an API
    key) and aren't already the primary. Embedding has no fallback in MainAI 0.1 — an
    inconsistent embedding provider mid-conversation would silently corrupt retrieval
    quality, so a failed embedding call should surface as an error, not swap providers.
    """
    settings = get_settings()
    primary_provider, primary_model = resolve_active(db, role="chat")
    chain = [(primary_provider, primary_model)]
    seen_names = {primary_provider.name}

    for name in [p.strip() for p in settings.chat_fallback_order.split(",") if p.strip()]:
        if name in seen_names or name not in _PROVIDERS:
            continue
        provider = get_provider(name)
        if not provider.is_configured():
            continue
        chain.append((provider, DEFAULT_CHAT_MODELS.get(name, primary_model)))
        seen_names.add(name)

    return chain


async def chat_with_fallback(
    db: Session,
    messages: list[Message],
    *,
    owner_id: uuid.UUID | None = None,
    purpose: str | None = None,
    requested_by: str | None = None,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    **kwargs,
) -> tuple[ChatResult, list[str]]:
    """Try the primary chat provider, then fall back through CHAT_FALLBACK_ORDER on failure.

    Returns the successful result plus the list of provider names that were attempted
    (including the one that succeeded), so callers can log/audit exactly what happened
    instead of it being invisible.

    Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V1): when
    `owner_id` is passed, every attempt is routed through `enforce_egress_policy()` first --
    default-deny gate, hard-deny on NEVER_EGRESS-marked content (never partial-send, and never
    treated as "try the next provider" -- the content itself is what's denied, not a specific
    provider being unreachable), otherwise secret-pattern redacted, with exactly one disclosure-
    ledger row per real attempt. `owner_id` is optional and defaults to None (gate skipped,
    unchanged legacy behavior) so existing callers migrate incrementally -- see the threat
    model's V1/V4 blocker list for which callers still need to pass it.
    """
    # Deferred import: app/providers/verification.py imports resolve_active from this module,
    # so importing it at module load time would be a circular import. classify_provider_exception
    # turns an exception into a safe (category, message) pair — used for EVERY log line below,
    # never str(exc)/%s-on-the-exception directly, since an httpx-originated exception embeds
    # the full request URL in its default message, and a provider can put its API key directly
    # in that URL (historically app/providers/gemini_provider.py's query-string auth — see its
    # own module docstring for why it no longer does that; this logging fix stands regardless
    # of which provider's URL shape changes in the future). Real incident, 2026-07-26: this
    # exact line logged a raw httpx.HTTPStatusError straight to the Docker log, and it happened
    # to contain a live Gemini key.
    from app.providers.verification import classify_provider_exception

    # Deferred for the same reason: keep app.egress_policy's own import graph (which reaches
    # into app.development_operator.service) from ever becoming a load-time dependency of this
    # already-central module.
    from app.egress_policy import EgressDeniedError, enforce_egress_policy

    chain = resolve_chat_chain(db)
    attempted: list[str] = []
    last_error: Exception | None = None

    for provider, model in chain:
        attempted.append(provider.name)
        try:
            outbound_messages = messages
            if owner_id is not None:
                payload = [{"role": m.role, "content": m.content} for m in messages]
                redacted = enforce_egress_policy(
                    db,
                    owner_id=owner_id,
                    provider=provider.name,
                    model=model,
                    purpose=purpose or "chat",
                    requested_by=requested_by or "providers.registry.chat_with_fallback",
                    payload=payload,
                    task_id=task_id,
                    goal_id=goal_id,
                    job_id=job_id,
                )
                outbound_messages = [Message(role=m["role"], content=m["content"]) for m in redacted]
            result = await provider.chat(outbound_messages, model=model, **kwargs)
            if len(attempted) > 1:
                logger.warning(
                    "Chat fallback engaged: %s failed over to %s (attempted: %s)",
                    attempted[0],
                    provider.name,
                    attempted,
                )
            return result, attempted
        except EgressDeniedError:
            # A denial is about the CONTENT, not this specific provider -- every other
            # provider in the chain would deny identically. Never treated as a per-provider
            # failure to fall back from; abort the whole call immediately.
            raise
        except Exception as exc:  # noqa: BLE001 - any provider failure should trigger fallback, not just ProviderError
            safe = classify_provider_exception(exc)
            logger.warning("Chat provider %s failed (%s): %s", provider.name, safe.result.value, safe.message)
            last_error = exc

    outcome = classify_provider_exception(last_error) if last_error is not None else None
    category = outcome.result.value if outcome else "unreachable"
    safe_detail = outcome.message if outcome else "Okänt fel inträffade."
    raise ProviderError(
        f"Alla konfigurerade leverantörer misslyckades ({', '.join(attempted)}). {safe_detail}",
        category=category,
    )
