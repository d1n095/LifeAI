from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    environment: str = "development"
    secret_key: str = "change-me-in-production"

    # Founder account — the single, permanent MainAI operator account, provisioned
    # automatically on first startup (see app/bootstrap.py, app/founder.py). Never a
    # self-registered account: public registration is disabled entirely (see
    # app/routers/auth.py's register()). Real values are entered directly in the Render
    # dashboard (sync: false in render.yaml) — never committed, never pasted in chat.
    founder_email: str = "founder@lifeos.local"
    founder_password: str = "change-me-in-production"

    # Auth — short-lived access token + long-lived rotating refresh token, both delivered as
    # HttpOnly cookies (never returned in a JSON body, never stored client-side). See
    # docs/AUTH_THREAT_MODEL.md.
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 14

    # Cookie attributes. Secure=True works on localhost/127.0.0.1 without HTTPS — browsers
    # treat those as a "secure context" by spec, so the same (safer) settings apply in dev
    # and prod rather than a weaker dev-only fallback that could leak into production.
    # SameSite=None is required because frontend and backend are different origins; that
    # trade-off is exactly why CSRF protection (app/csrf.py) is mandatory, not optional.
    cookie_secure: bool = True
    cookie_samesite: str = "none"
    cookie_domain: str | None = None  # unset = host-only cookie; set only for a shared parent domain

    # Rate limiting (requests per minute, per authenticated user; login/refresh/logout are
    # keyed by IP since there's no authenticated user yet at that point)
    rate_limit_chat_per_minute: int = 20
    rate_limit_default_per_minute: int = 120
    # Import is heavier than ordinary CRUD (a ZIP can contain up to 500 files, each chunked
    # and embedded — see app/rag/zip_import.py/app/rag/library_import.py) and, unlike chat,
    # has no per-request AI-provider cost ceiling of its own to lean on, so it gets its own,
    # stricter limit rather than sharing rate_limit_default_per_minute.
    rate_limit_library_import_per_minute: int = 10
    # Founder Workbench analysis calls a real AI provider (same per-request cost profile as
    # chat) but is a heavier prompt (question + retrieved context + a critique pass) — kept
    # equal to chat's default rather than looser, since there's no cheaper fallback path.
    rate_limit_workbench_per_minute: int = 20
    rate_limit_login_per_minute: int = 10
    rate_limit_refresh_per_minute: int = 30
    rate_limit_logout_per_minute: int = 30
    # Deliberately stricter than login — registration and password-reset requests are the
    # cheapest way to spam another person's inbox or mass-probe for valid accounts.
    rate_limit_register_per_minute: int = 5
    rate_limit_forgot_password_per_minute: int = 5
    rate_limit_verify_email_per_minute: int = 20
    rate_limit_reset_password_per_minute: int = 10

    # Account/email flow
    email_verification_token_expire_hours: int = 24
    password_reset_token_expire_hours: int = 1
    # Base URL used to build the links inside verification/reset emails — deliberately a
    # separate setting from frontend_origins (a CORS allow-list) so an operator can't
    # accidentally break email links by editing the CORS config, or vice versa.
    public_app_url: str = "http://localhost:3000"

    # SMTP — if smtp_host is unset, app/email.py falls back to a local dev-only outbox
    # (never the application log — see app/email.py for why) instead of sending it.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    # Two mutually exclusive connection modes (see app/email.py): STARTTLS (plaintext
    # connect, then upgrade — typically port 587) vs. implicit TLS/SSL (TLS from the first
    # byte — typically port 465, e.g. Strato's smtp.strato.com:465). Both default False/True
    # to match the pre-existing STARTTLS-only behavior for anyone with an existing .env;
    # app/main.py's _check_smtp_configured() rejects the contradictory combination
    # (both true) at startup rather than silently picking one.
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_from_email: str = "no-reply@lifeos.local"
    smtp_from_name: str = "MainAI / Life OS"
    # Opt-in only, local dev convenience: if set (and environment != "production"), an
    # unsent email (no SMTP configured) is written as a plain-text file here instead of
    # being silently dropped. Never set in production — prefer pointing SMTP at a real
    # local mail catcher (see the "dev-mail" docker-compose profile / docs/OPERATIONS.md).
    dev_mail_outbox_dir: str | None = None

    # Rate limiting storage: unset (default) uses slowapi's in-memory backend, which is
    # correct for exactly one backend process and resets on restart — a real deployment with
    # more than one replica needs a shared backend so the limit is enforced across all of
    # them and survives individual restarts. Set to a redis:// URL to switch (see
    # app/limiter.py). docker-compose.yml's `backend` service sets this by default.
    redis_url: str | None = None

    # Token cleanup (app/cleanup.py): how long expired/revoked/used auth tokens are kept
    # before being purged, for security-incident forensics (e.g. investigating a
    # refresh-token-reuse event after the fact) — not kept forever, since the metadata
    # (IP, user-agent, timestamps) has no purpose past that window.
    token_cleanup_retention_days: int = 30
    enable_scheduled_cleanup: bool = True
    cleanup_interval_hours: int = 24

    # Database — two roles by design:
    # `database_url` is the superuser (POSTGRES_USER) and is used ONLY for schema migrations
    # and enabling Row-Level Security, both of which require owner/superuser privileges.
    # `app_database_url` is a restricted, non-superuser role (see backend/db-init/) used for
    # all runtime request handling — Postgres superusers bypass RLS unconditionally, so if the
    # app queried through the superuser, RLS would be silently ineffective.
    database_url: str = "postgresql://lifeos:lifeos@postgres:5432/lifeos"
    app_database_url: str = "postgresql://mainai_app:mainai_app@postgres:5432/lifeos"

    # Embedding vectors are stored in Postgres via pgvector (app/models/document_chunk.py),
    # not a separate vector database — embedding_dim is a real, fixed constraint (the
    # column's declared dimension, set at migration time), not just a default: switching to
    # an embedding model with a different vector size needs a new migration, not just this
    # value changed. See app/rag/vector_store.py.
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

    # CORS — comma-separated explicit allow-list, never a wildcard (incompatible with
    # allow_credentials=True anyway, which cookie-based auth requires).
    frontend_origins: str = "http://localhost:3000"

    # Life Library durable-worker package (app/storage/) — the persistent Docker volume
    # original files are streamed into. Must be the SAME filesystem/volume across the
    # backend and worker containers (see docker-compose.vps.yml), since app/storage/
    # local_fs.py's atomic write relies on a same-filesystem rename from its own tmp/
    # subdirectory — never point this at the container's own /tmp (a separate tmpfs on the
    # VPS, see docker-compose.vps.yml's read_only+tmpfs config).
    storage_root: str = "/var/lib/lifeai/uploads"

    # Worker (app/worker.py): how often an idle worker polls for a new/reclaimable job, how
    # long a claimed job's lease lasts before another worker may reclaim it as abandoned, and
    # how many jobs one worker process handles at once. Kept low by default — this package's
    # explicit "låg resursbudget" requirement for the current VPS — but configurable per
    # docs/VPS_OPERATIONS_RUNBOOK.md if the server is upgraded later.
    worker_poll_interval_seconds: float = 2.0
    worker_lease_seconds: int = 120
    worker_concurrency: int = 1
    # Identifies which worker process/container claimed a job (GET /api/library/ops/status,
    # troubleshooting) — defaults to the container's own hostname (stable per-container,
    # unique per replica) rather than a random id, so a founder reading ops status or logs
    # can tell which container to look at.
    worker_id: str | None = None

    @property
    def frontend_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]

    # Fail fast on a malformed connection string at process startup (when Settings() is
    # first constructed) rather than surfacing as a confusing low-level driver error deep
    # inside SQLAlchemy/redis-py the first time something actually tries to connect.
    # Deliberately checks *shape* only (scheme + host present, expected scheme family) — not
    # reachability, which is what app/main.py's _check_redis_reachable() and the DB
    # connection itself already verify separately.
    @field_validator("database_url", "app_database_url")
    @classmethod
    def _validate_postgres_url(cls, value: str, info) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
            raise ValueError(
                f"{info.field_name.upper()} ser inte ut som en giltig Postgres-anslutningssträng "
                f"(förväntar postgresql://användare:lösenord@host:port/databas)."
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if parts.scheme not in ("redis", "rediss") or not parts.hostname:
            raise ValueError(
                "REDIS_URL ser inte ut som en giltig Redis-anslutningssträng (förväntar "
                "redis://... eller rediss://... med ett host)."
            )
        return value

    @field_validator("public_app_url")
    @classmethod
    def _validate_public_app_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(
                "PUBLIC_APP_URL ser inte ut som en giltig URL (förväntar http(s)://host, "
                "används i verifierings-/återställningsmail-länkar)."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
