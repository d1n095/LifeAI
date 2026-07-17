from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.provider_config import ProviderConfig
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import LLMProvider, ProviderError
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.openrouter_provider import OpenRouterProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "deepseek": DeepSeekProvider,
    "openrouter": OpenRouterProvider,
    "ollama": OllamaProvider,
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
