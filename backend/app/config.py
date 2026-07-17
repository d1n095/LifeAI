from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    environment: str = "development"
    secret_key: str = "change-me-in-production"

    # Bootstrap admin account (created automatically on first startup if no users exist)
    admin_email: str = "admin@lifeos.local"
    admin_password: str = "change-me-in-production"

    # Auth
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 12

    # Rate limiting (requests per minute, per authenticated user)
    rate_limit_chat_per_minute: int = 20
    rate_limit_default_per_minute: int = 120

    # Database — two roles by design:
    # `database_url` is the superuser (POSTGRES_USER) and is used ONLY for schema migrations
    # and enabling Row-Level Security, both of which require owner/superuser privileges.
    # `app_database_url` is a restricted, non-superuser role (see backend/db-init/) used for
    # all runtime request handling — Postgres superusers bypass RLS unconditionally, so if the
    # app queried through the superuser, RLS would be silently ineffective.
    database_url: str = "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    app_database_url: str = "postgresql://mainai_app:mainai_app@postgres:5432/lifeos"

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

    # Fallback order per role: comma-separated provider names, tried in order after the
    # active provider fails. MainAI 0.1 focuses this on OpenAI/Anthropic/Gemini.
    chat_fallback_order: str = "openai,anthropic,gemini"

    # CORS
    frontend_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
