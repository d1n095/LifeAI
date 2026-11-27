from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validated_zai_base_url(value: str) -> str:
    """Only supported HTTPS API endpoints may receive the Z.ai credential.

    Fixed origins exclude arbitrary proxies, private IPs and metadata endpoints.
    The adapter separately disables redirects and environment proxy inheritance.
    """
    if any(ord(c) <= 32 for c in value):
        raise ValueError("Z.ai requires a supported HTTPS API endpoint")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "api.z.ai"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in ("/api/paas/v4", "/api/coding/paas/v4")):
        raise ValueError("Z.ai requires a supported HTTPS API endpoint")
    return value.rstrip("/")


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

    # Instruction-aware embedding models (Qwen3-Embedding and friends) expect the RETRIEVAL
    # QUERY to carry an instruction prefix while DOCUMENTS stay bare — that asymmetry is how
    # they were trained, and applying it only to the query channel is what makes their local
    # retrieval correct (a document that stored the prefix would be embedded off-distribution).
    # Empty by default: symmetric models (nomic-embed-text, all-minilm, text-embedding-3-*) are
    # used unmodified, and this can never silently change an established embedding space unless
    # an operator opts in. Applied in app/rag/query_embedding.py.
    embedding_query_prefix: str = ""

    # Active provider (can be overridden at runtime via admin API -> DB config)
    default_llm_provider: str = "openai"
    default_llm_model: str = "gpt-4o-mini"

    # Budget for ONE provider-assisted Safe Planner call. A hosted API answers in seconds;
    # a local-only deployment runs inference on the same CPU as the worker, which is a
    # different latency class entirely. Measured on the 2026-11-27 local stack: llama3.1
    # evaluates ~22 prompt tokens/s and generates ~6.5 tokens/s, so one complete candidate
    # takes 3-5 minutes. This is the hosted default; in LOCAL_ONLY_MODE the effective bound is
    # raised to LOCAL_ONLY_PLANNING_TIMEOUT_SECONDS (app/provider_planning/service.py) and is
    # never lowered below this value.
    provider_planning_timeout_seconds: int = 60
    default_embedding_provider: str = "openai"
    default_embedding_model: str = "text-embedding-3-small"

    # Provider API keys (all optional — only the ones you configure are usable)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    deepseek_api_key: str | None = None
    openrouter_api_key: str | None = None
    zai_api_key: str | None = None
    zai_base_url: str = "https://api.z.ai/api/paas/v4"
    @field_validator("zai_base_url")
    @classmethod
    def validate_zai_endpoint(cls, value: str) -> str:
        return validated_zai_base_url(value)

    ollama_base_url: str = "http://ollama:11434"

    # Fallback order per role: comma-separated provider names, tried in order after the
    # active provider fails. MainAI 0.1 focuses this on OpenAI/Anthropic/Gemini; local
    # ollama is the LAST-RESORT fallback so a deployment with zero remote API keys still
    # has a usable local model (local-AI-primary rule — OpenAI consultation is the bounded
    # exception, never the default). resolve_chat_chain demotes unconfigured providers,
    # so a keyless deployment's chain head becomes the local model.
    chat_fallback_order: str = "openai,anthropic,gemini,ollama"

    # Founder directive (Task #60, requirement 8): when true, app/providers/registry.py's
    # chat_with_fallback() refuses to fall back to any non-local provider (today: everything
    # except "ollama") -- a local-provider failure must surface as an error, never silently
    # escalate to a real external provider call. No authorization envelope/context can
    # override this; it is a deployment-level posture, not a per-call exception. Defaults to
    # False (the historical, still-default behavior) so this is strictly additive.
    local_only_mode: bool = False

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

    # MainAI Project Memory & Coordination Loop (app/project_memory.py): filesystem root the
    # doc-ingestion endpoint reads governing sources (CLAUDE.md, docs/BRANCH_REGISTRY.md,
    # docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md) and git state from. Empty by default —
    # deliberately NOT the container's own working directory, since a production backend
    # image only ever contains backend/ (see Dockerfile), not the outer repo checkout. Only
    # meaningful in a dev/CI/agent-session context where the full repo is actually present;
    # doc/git ingestion is a no-op (raises a clear error) when unset.
    project_root: str = ""

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

    # P1 (provider pre-flight verification, app/providers/verification.py): how long a
    # verification result (ok or a failure) is trusted before a new real call is made —
    # keeps a large batch/worker poll loop from hammering a provider that's genuinely down
    # once per file. Short on purpose: an operator fixing a key shouldn't have to wait long
    # for the worker to notice. Separate, much shorter timeout for the verification call
    # itself than production chat/embed calls (60-120s) — a pre-flight check must fail fast,
    # never hang an import waiting to find out a provider is unreachable.
    provider_verification_cache_seconds: int = 300
    provider_verification_timeout_seconds: float = 10.0
    # Local runtimes (Ollama) need a longer pre-flight budget: the FIRST call after an idle
    # period (or after a restart) loads the model weights into memory, which routinely takes
    # longer than the 10s a hosted API ever needs. Kept separate so the fast-fail contract for
    # cloud providers is untouched (app/providers/verification.py:verification_timeout_seconds).
    local_verification_timeout_seconds: float = 120.0

    # MainAI Core agent orchestration (app/agent_orchestration.py, app/integrations/
    # github_client.py): a GitHub Personal Access Token or App installation token scoped to
    # `github_repo` ("owner/repo"), used ONLY when github_write_enabled is explicitly turned
    # on. Same optional-until-configured pattern as the provider API keys above — real values
    # are entered directly in the Render dashboard, never committed, never pasted in chat.
    # `github_write_enabled=False` (the default) means MainAI only ever PROPOSES an exact PR
    # (branch name, commit message, diff, PR body) without calling GitHub at all — see
    # prepare_github_pr(). `github_auto_merge_enabled` gates a capability that, as of this
    # slice, has no implementation to gate: app/integrations/github_client.py deliberately has
    # no merge method at all, so there is no code path this flag could unsafely enable yet —
    # it exists so a future real merge implementation has an explicit off-by-default switch to
    # check, per CLAUDE.md's "Implementera mergekapaciteten bakom en avstängd feature flag".
    github_token: str | None = None
    github_repo: str | None = None  # "owner/repo", e.g. "d1n095/LifeAI"
    github_write_enabled: bool = False
    github_auto_merge_enabled: bool = False

    # Proposal-mode isolation (app/mainai_execution/proposal_worktree.py): when on (the default),
    # a `repo_edit` proposal is written into its own local-only git checkout, bootstrapped from
    # this checkout's HEAD by a filesystem-path fetch — no remote, no token, no write path back to
    # the live tree. Turning this off restores the pre-isolation behaviour, where PI-1d's rollback
    # is the only protection; that fallback also engages automatically (with a warning) if a
    # bootstrap cannot be built at all, so a missing `git` degrades to "as safe as before" rather
    # than "MainAI cannot propose anything".
    mainai_proposal_worktree_isolation: bool = True

    # Founder Alpha R14: READ-ONLY development observation. `local_repo_path` is optional and
    # only ever read (git rev-parse/log/rev-list) — never written, never pushed.
    # `github_tracked_branch` selects the branch the snapshot describes (None = GitHub's own
    # default branch). `github_status_stale_minutes` is when a verified picture is reported as
    # stale instead of quietly staying current.
    github_tracked_branch: str | None = None
    github_status_stale_minutes: int = 30
    local_repo_path: str | None = None

    # Direct OpenAI consultation (app/openai_consult/) — lets MainAI/the founder call an
    # OpenAI model through the official Responses API without anyone manually forwarding
    # messages. Same optional-until-configured + off-by-default pattern as github_write_enabled
    # above: EVERY gate below fails closed, so the integration is inert until an operator
    # explicitly turns it on, allowlists at least one model, and provides a real API key.
    #   openai_consult_enabled           master switch — False means no code path in this
    #                                    module ever reaches the network, full stop.
    #   openai_consult_allowed_models    comma-separated exact model ids; EMPTY deny-lists
    #                                    everything even when enabled=True.
    #   openai_consult_max_output_tokens hard per-request ceiling; a request may lower it,
    #                                    never raise it.
    #   openai_consult_timeout_seconds   httpx connect/read timeout for the outbound call.
    #   openai_consult_requests_per_minute  slowapi per-user HTTP rate limit on the endpoint.
    #   openai_consult_daily_token_budget   per-user rolling UTC-day token ceiling
    #                                    (reserved before the call, settled with real usage
    #                                    after — see app/openai_consult/budgets.py).
    #   openai_consult_daily_cost_budget_usd  per-user rolling UTC-day cost ceiling in USD,
    #                                    computed from app/providers/pricing.py's table; a
    #                                    model without a pricing entry is refused outright
    #                                    (fail-closed — an unpriceable model is an unbudgetable
    #                                    one). Deliberately a string, parsed as Decimal, so
    #                                    env-provided values keep exact decimal semantics.
    #   openai_consult_web_search_enabled  the ONLY tool this integration can ever attach:
    #                                    OpenAI's hosted read-only web_search. False (the
    #                                    default) means the request body never contains a
    #                                    tools field at all. Client-supplied tools are
    #                                    structurally refused — there is no code path to
    #                                    shell, filesystem, GitHub or code-execution tools.
    #   openai_consult_store_responses   passed as the Responses API `store` flag; False
    #                                    (default) keeps prompt/response content out of
    #                                    OpenAI's stored-response surface.
    openai_consult_enabled: bool = False
    openai_consult_allowed_models: str = ""
    openai_consult_max_output_tokens: int = 1024
    openai_consult_timeout_seconds: float = 45.0
    openai_consult_requests_per_minute: int = 10
    openai_consult_daily_token_budget: int = 200000
    openai_consult_daily_cost_budget_usd: str = "5.00"
    openai_consult_web_search_enabled: bool = False
    openai_consult_store_responses: bool = False
    # Bounded consultation-selection policy (app/openai_consult/policy.py): how many
    # MainAI-initiated consultations one owner may ADMIT per UTC day. Consultation is a
    # bounded exception to the local-AI-primary rule, not an unbounded reflex; 20 is a
    # deliberately modest ceiling and the counter is Redis-backed (fail-closed in
    # production without it) so replicas cannot each spend the cap.
    openai_consult_mainai_daily_call_cap: int = 20
    # Independent switch for the MainAI-invoked path (app/openai_consult/mainai.py +
    # workforce/provider_worker.py's consultation_mode): MainAI orchestration may consult
    # OpenAI ONLY when BOTH openai_consult_enabled AND this flag are true AND the workforce
    # activation gates are verified. Deliberately separate from the founder HTTP path so
    # either capability can be disabled without touching the other. Local/in-process workers
    # stay the primary execution path — this flag never changes any default routing.
    openai_consult_mainai_enabled: bool = False

    # Secure founder account recovery (app/founder_recovery.py) — fail-closed by default:
    # disabled and no code configured. The recovery CODE itself is never stored; only its
    # SHA-256 lives here, so even a leaked configuration does not yield a usable code.
    # Initiation is CLI-only (server shell access) — there is no HTTP initiation surface.
    founder_recovery_enabled: bool = False
    founder_recovery_code_sha256: str = ""
    founder_recovery_token_expire_hours: int = 1

    # --- Revenue & Compute Engine (app/billing/, migration 0070) ----------------------
    # Enforcement OFF by default: turning it on makes budget refusals user-visible, which
    # is a founder decision (see docs/REVENUE_COMPUTE_ENGINE.md). When ON and the counter
    # store (Redis) is unavailable in production, spending FAILS CLOSED — never admitted
    # uncounted. Caps are USD/day and USD/month per tier (None = unlimited; only
    # enterprise may be configured unlimited, deliberately).
    billing_enforcement_enabled: bool = False
    billing_default_tier: str = "paid"
    billing_free_daily_cost_usd: float = 0.10
    billing_free_monthly_cost_usd: float = 1.50
    billing_free_daily_token_cap: int = 30000
    billing_free_monthly_token_cap: int = 300000
    billing_paid_daily_cost_usd: float = 2.00
    billing_paid_monthly_cost_usd: float = 25.00
    billing_paid_daily_token_cap: int = 300000
    billing_paid_monthly_token_cap: int = 3000000
    billing_enterprise_daily_cost_usd: float = 20.00
    billing_enterprise_monthly_cost_usd: float = 250.00
    billing_enterprise_daily_token_cap: int = 3000000
    billing_enterprise_monthly_token_cap: int = 30000000
    # A provider/model with no price entry still counts AGAINST cost budgets at this
    # conservative fallback rate (per 1k tokens) instead of passing unpriced.
    billing_unknown_cost_fallback_usd_per_1k: float = 0.01
    # Local compute infrastructure model (platform cost — users' local inference is free).
    billing_local_gpu_hour_usd: float = 0.75
    billing_local_gpu_tokens_per_second: float = 30.0
    billing_local_energy_overhead_pct: float = 20.0
    # Allocation margin: fraction of SETTLED revenue withheld from spending ceilings.
    billing_safety_margin_pct: int = 25

    # --- Advertising subsystem (app/ads/, migration 0071) -----------------------------
    # DISABLED BY DEFAULT, provider-agnostic, and structurally firewalled from private
    # context (see app/ads/__init__.py). Enabling in production additionally requires an
    # explicit ad source; there is no live ad-network integration in this slice.
    ads_enabled: bool = False
    ads_house_ads_json: str | None = None  # operator-supplied house-ad pool, JSON
    ads_max_impressions_per_user_per_day: int = 20
    ads_max_clicks_per_user_per_day: int = 5
    ads_click_min_interval_seconds: int = 3  # impression→click plausibility floor

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
