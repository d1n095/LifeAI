from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    environment: str = "development"
    secret_key: str = "change-me-in-production"
    admin_password: str = "change-me-in-production"

    # Database
    database_url: str = "postgresql://lifeos:lifeos@postgres:5432/lifeos"

    # Vector DB
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "lifeos_knowledge"
    embedding_dim: int = 1536

    # Active provider (can be overridden at runtime via admin API -> DB config)
    default_llm_provider: str = "openai"
    default_llm_model: str = "gpt-4o-mini"
    default_embedding_provider: str = "openai"
    default_embedding_model: str = "text-embedding-3-small"

    # Provider API keys (all optional — only the ones you configure are usable)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    deepseek_api_key: str | None = None
    openrouter_api_key: str | None = None
    ollama_base_url: str = "http://ollama:11434"

    # CORS
    frontend_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
