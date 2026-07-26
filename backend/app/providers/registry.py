import logging

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


async def chat_with_fallback(db: Session, messages: list[Message], **kwargs) -> tuple[ChatResult, list[str]]:
    """Try the primary chat provider, then fall back through CHAT_FALLBACK_ORDER on failure.

    Returns the successful result plus the list of provider names that were attempted
    (including the one that succeeded), so callers can log/audit exactly what happened
    instead of it being invisible.
    """
    chain = resolve_chat_chain(db)
    attempted: list[str] = []
    last_error: Exception | None = None

    for provider, model in chain:
        attempted.append(provider.name)
        try:
            result = await provider.chat(messages, model=model, **kwargs)
            if len(attempted) > 1:
                logger.warning(
                    "Chat fallback engaged: %s failed over to %s (attempted: %s)",
                    attempted[0],
                    provider.name,
                    attempted,
                )
            return result, attempted
        except Exception as exc:  # noqa: BLE001 - any provider failure should trigger fallback, not just ProviderError
            logger.warning("Chat provider %s failed: %s", provider.name, exc)
            last_error = exc

    # Deferred import: app/providers/verification.py imports resolve_active from this module,
    # so importing it at module load time would be a circular import. classify_provider_exception
    # turns last_error into a safe (category, message) pair — never str(exc) directly, since an
    # httpx-originated exception can carry the full request URL, and
    # app/providers/gemini_provider.py puts the API key in that URL's query string. Every caller
    # of chat_with_fallback (chat.py, workbench.py, agent_orchestration.py) gets this safety for
    # free rather than each needing its own classification.
    from app.providers.verification import classify_provider_exception

    outcome = classify_provider_exception(last_error) if last_error is not None else None
    category = outcome.result.value if outcome else "unreachable"
    safe_detail = outcome.message if outcome else "Okänt fel inträffade."
    raise ProviderError(
        f"Alla konfigurerade leverantörer misslyckades ({', '.join(attempted)}). {safe_detail}",
        category=category,
    )
