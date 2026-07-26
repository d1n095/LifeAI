"""A machine-checked contract for every environment variable app/config.py's Settings
exposes — built for PRIO 2 of the 2026-07-19/20 night shift work order.

CONFIG_MATRIX is the single source of truth this file checks Settings against. If a field
is added to Settings without a matching entry here, test_every_settings_field_is_documented
fails — the matrix cannot silently drift out of sync with the actual config surface.

This file never imports or prints a real secret value. Every URL/password used below is a
fake, hardcoded test fixture."""

from urllib.parse import urlsplit

import pytest

from app.config import Settings

# path: (required_in_production, secret, default_repr, validates)
#   required_in_production: True if production MUST have a real, non-default value; False if
#     optional or if the shipped default is already a safe production value.
#   secret: True if this must never appear in logs, error messages, or committed files.
#   default_repr: the literal default from Settings (redacted with "***" if secret and
#     non-empty, so this file itself never contains anything resembling a real credential).
#   validates: short description of what, if anything, enforces correctness (pydantic
#     validator, startup check in app/main.py, or "none" if nothing currently does).
CONFIG_MATRIX: dict[str, dict] = {
    "environment": {
        "required_in_production": False,
        "secret": False,
        "default": "development",
        "validates": "none — plain string, compared by value where it matters (== 'production')",
    },
    "secret_key": {
        "required_in_production": True,
        "secret": True,
        "default": "***",
        "validates": "_check_no_placeholder_secrets (app/main.py) — rejects the shipped default in production",
    },
    "founder_email": {
        "required_in_production": True,
        "secret": False,
        "default": "founder@lifeos.local",
        "validates": "_check_no_placeholder_secrets (app/main.py) — rejects the shipped default in production",
    },
    "founder_password": {
        "required_in_production": True,
        "secret": True,
        "default": "***",
        "validates": "_check_no_placeholder_secrets (app/main.py) — rejects the shipped default in production",
    },
    "jwt_algorithm": {"required_in_production": False, "secret": False, "default": "HS256", "validates": "none"},
    "access_token_expire_minutes": {"required_in_production": False, "secret": False, "default": 15, "validates": "none"},
    "refresh_token_expire_days": {"required_in_production": False, "secret": False, "default": 14, "validates": "none"},
    "cookie_secure": {
        "required_in_production": False,  # default (True) is already the safe production value
        "secret": False,
        "default": True,
        "validates": "_check_cookies_secure (app/main.py) — rejects False in production",
    },
    "cookie_samesite": {"required_in_production": False, "secret": False, "default": "none", "validates": "none"},
    "cookie_domain": {"required_in_production": False, "secret": False, "default": None, "validates": "none"},
    "rate_limit_chat_per_minute": {"required_in_production": False, "secret": False, "default": 20, "validates": "none"},
    "rate_limit_library_import_per_minute": {"required_in_production": False, "secret": False, "default": 10, "validates": "none"},
    "rate_limit_workbench_per_minute": {"required_in_production": False, "secret": False, "default": 20, "validates": "none"},
    "rate_limit_default_per_minute": {"required_in_production": False, "secret": False, "default": 120, "validates": "none"},
    "rate_limit_login_per_minute": {"required_in_production": False, "secret": False, "default": 10, "validates": "none"},
    "rate_limit_refresh_per_minute": {"required_in_production": False, "secret": False, "default": 30, "validates": "none"},
    "rate_limit_logout_per_minute": {"required_in_production": False, "secret": False, "default": 30, "validates": "none"},
    "rate_limit_register_per_minute": {"required_in_production": False, "secret": False, "default": 5, "validates": "none"},
    "rate_limit_forgot_password_per_minute": {"required_in_production": False, "secret": False, "default": 5, "validates": "none"},
    "rate_limit_verify_email_per_minute": {"required_in_production": False, "secret": False, "default": 20, "validates": "none"},
    "rate_limit_reset_password_per_minute": {"required_in_production": False, "secret": False, "default": 10, "validates": "none"},
    "email_verification_token_expire_hours": {"required_in_production": False, "secret": False, "default": 24, "validates": "none"},
    "password_reset_token_expire_hours": {"required_in_production": False, "secret": False, "default": 1, "validates": "none"},
    "public_app_url": {
        "required_in_production": True,
        "secret": False,
        "default": "http://localhost:3000",
        "validates": "_validate_public_app_url (app/config.py, pydantic field_validator) — rejects a malformed URL",
    },
    "smtp_host": {
        "required_in_production": True,
        "secret": False,
        "default": None,
        "validates": "_check_smtp_configured (app/main.py) — rejects unset in production",
    },
    "smtp_port": {"required_in_production": False, "secret": False, "default": 587, "validates": "none"},
    "smtp_username": {"required_in_production": False, "secret": False, "default": None, "validates": "none"},
    "smtp_password": {"required_in_production": False, "secret": True, "default": None, "validates": "none"},
    "smtp_use_tls": {
        "required_in_production": False,
        "secret": False,
        "default": True,
        "validates": "_check_smtp_mode (app/main.py) — rejects smtp_use_tls AND smtp_use_ssl both true",
    },
    "smtp_use_ssl": {
        "required_in_production": False,
        "secret": False,
        "default": False,
        "validates": "_check_smtp_mode (app/main.py) — rejects smtp_use_tls AND smtp_use_ssl both true",
    },
    "smtp_from_email": {"required_in_production": False, "secret": False, "default": "no-reply@lifeos.local", "validates": "none"},
    "smtp_from_name": {"required_in_production": False, "secret": False, "default": "MainAI / Life OS", "validates": "none"},
    "dev_mail_outbox_dir": {"required_in_production": False, "secret": False, "default": None, "validates": "app/email.py refuses to use this when environment == 'production', even if set"},
    "redis_url": {
        "required_in_production": False,  # optional (falls back to in-memory rate limiting), but see docs/OPERATIONS.md re: multi-replica correctness
        "secret": True,
        "default": None,
        "validates": "_validate_redis_url (app/config.py) shape check; _check_redis_reachable (app/main.py) reachability check when set",
    },
    "token_cleanup_retention_days": {"required_in_production": False, "secret": False, "default": 30, "validates": "none"},
    "enable_scheduled_cleanup": {"required_in_production": False, "secret": False, "default": True, "validates": "none"},
    "cleanup_interval_hours": {"required_in_production": False, "secret": False, "default": 24, "validates": "none"},
    "database_url": {
        "required_in_production": True,
        "secret": True,
        "default": "***",
        "validates": "_validate_postgres_url (app/config.py, pydantic field_validator) — rejects a malformed URL",
    },
    "app_database_url": {
        "required_in_production": True,
        "secret": True,
        "default": "***",
        "validates": "_validate_postgres_url (app/config.py, pydantic field_validator) — rejects a malformed URL",
    },
    "embedding_dim": {"required_in_production": False, "secret": False, "default": 1536, "validates": "none — fixed by the pgvector column, changing it needs a new migration"},
    "default_llm_provider": {"required_in_production": False, "secret": False, "default": "openai", "validates": "none — an unconfigured/unknown provider surfaces at request time via ProviderError, not at startup, see app/providers/registry.py"},
    "default_llm_model": {"required_in_production": False, "secret": False, "default": "gpt-4o-mini", "validates": "none"},
    "default_embedding_provider": {"required_in_production": False, "secret": False, "default": "openai", "validates": "none"},
    "default_embedding_model": {"required_in_production": False, "secret": False, "default": "text-embedding-3-small", "validates": "none"},
    "openai_api_key": {"required_in_production": False, "secret": True, "default": None, "validates": "none — provider is simply unavailable if unset (graceful degradation, see /api/admin/providers/status)"},
    "anthropic_api_key": {"required_in_production": False, "secret": True, "default": None, "validates": "none — see openai_api_key"},
    "google_api_key": {"required_in_production": False, "secret": True, "default": None, "validates": "none — see openai_api_key"},
    "deepseek_api_key": {"required_in_production": False, "secret": True, "default": None, "validates": "none — see openai_api_key"},
    "openrouter_api_key": {"required_in_production": False, "secret": True, "default": None, "validates": "none — see openai_api_key"},
    "ollama_base_url": {"required_in_production": False, "secret": False, "default": "http://ollama:11434", "validates": "none"},
    "chat_fallback_order": {"required_in_production": False, "secret": False, "default": "openai,anthropic,gemini", "validates": "none"},
    "frontend_origins": {"required_in_production": True, "secret": False, "default": "http://localhost:3000", "validates": "none — CORSMiddleware simply rejects any origin not in the list"},
    "storage_root": {"required_in_production": True, "secret": False, "default": "/var/lib/lifeai/uploads", "validates": "none — LocalFilesystemStorage fails at write time if the directory isn't writable, not at startup"},
    "project_root": {"required_in_production": False, "secret": False, "default": "", "validates": "none — app/project_memory.py raises a clear ValueError at call time when unset, never at startup"},
    "worker_poll_interval_seconds": {"required_in_production": False, "secret": False, "default": 2.0, "validates": "none — used as-is by app/worker.py's poll loop"},
    "worker_lease_seconds": {"required_in_production": False, "secret": False, "default": 120, "validates": "none — used as-is by app/jobs/lease.py's claim/renew queries"},
    "worker_concurrency": {"required_in_production": False, "secret": False, "default": 1, "validates": "none — not yet enforced as a hard cap, see app/worker.py's known limitations"},
    "worker_id": {"required_in_production": False, "secret": False, "default": None, "validates": "none — falls back to socket.gethostname() when unset, see app/worker.py's _worker_id"},
    "provider_verification_cache_seconds": {"required_in_production": False, "secret": False, "default": 300, "validates": "none — used as-is by app/providers/verification.py's ensure_verified"},
    "provider_verification_timeout_seconds": {"required_in_production": False, "secret": False, "default": 10.0, "validates": "none — passed as-is to provider.chat()/embed()'s timeout kwarg"},
}


def test_every_settings_field_is_documented():
    """Prevents drift: a field added to Settings without a matching CONFIG_MATRIX entry (or
    vice versa) fails this test immediately, instead of the matrix silently going stale."""
    settings_fields = set(Settings.model_fields.keys())
    matrix_fields = set(CONFIG_MATRIX.keys())
    missing_from_matrix = settings_fields - matrix_fields
    stale_in_matrix = matrix_fields - settings_fields
    assert not missing_from_matrix, f"Settings fields with no CONFIG_MATRIX entry: {missing_from_matrix}"
    assert not stale_in_matrix, f"CONFIG_MATRIX entries for fields no longer in Settings: {stale_in_matrix}"


def test_no_real_looking_secret_literals_in_this_file():
    """The matrix itself must never carry anything that looks like a real credential — only
    "***", None, or a known-safe non-secret default is allowed for fields marked secret."""
    for name, entry in CONFIG_MATRIX.items():
        if entry["secret"] and entry["default"] not in ("***", None):
            pytest.fail(f"{name} is marked secret but CONFIG_MATRIX default is not redacted: {entry['default']!r}")


class TestPostgresUrlValidation:
    def test_rejects_non_postgres_scheme(self):
        with pytest.raises(ValueError, match="DATABASE_URL"):
            Settings(database_url="mysql://user:pw@host:3306/db")

    def test_rejects_missing_host(self):
        with pytest.raises(ValueError, match="DATABASE_URL"):
            Settings(database_url="postgresql:///db")

    def test_accepts_well_formed_url(self):
        s = Settings(database_url="postgresql://user:pw@host:5432/db")
        assert s.database_url == "postgresql://user:pw@host:5432/db"

    def test_app_database_url_uses_the_same_rule(self):
        with pytest.raises(ValueError, match="APP_DATABASE_URL"):
            Settings(app_database_url="not-a-url-at-all")


class TestRedisUrlValidation:
    def test_none_is_valid_redis_is_optional(self):
        s = Settings(redis_url=None)
        assert s.redis_url is None

    def test_rejects_wrong_scheme(self):
        with pytest.raises(ValueError, match="REDIS_URL"):
            Settings(redis_url="http://host:6379")

    def test_accepts_redis_and_rediss_schemes(self):
        assert Settings(redis_url="redis://host:6379/0").redis_url == "redis://host:6379/0"
        assert Settings(redis_url="rediss://:pw@host:6379").redis_url == "rediss://:pw@host:6379"


class TestPublicAppUrlValidation:
    def test_rejects_missing_scheme(self):
        with pytest.raises(ValueError, match="PUBLIC_APP_URL"):
            Settings(public_app_url="justahostname.com")

    def test_accepts_https_url(self):
        assert Settings(public_app_url="https://lifeai-1.onrender.com").public_app_url == "https://lifeai-1.onrender.com"


def test_matrix_urls_all_actually_parse():
    """Sanity check on the matrix's own non-secret example/default values (e.g.
    ollama_base_url, public_app_url's default) — they should themselves be parseable URLs
    where the field name implies a URL, catching a typo in this file."""
    url_like_fields = ["public_app_url", "ollama_base_url"]
    for field in url_like_fields:
        default = CONFIG_MATRIX[field]["default"]
        if default is None or default == "***":
            continue
        parts = urlsplit(default)
        assert parts.scheme and parts.hostname, f"{field}'s matrix default {default!r} is not a valid URL"
