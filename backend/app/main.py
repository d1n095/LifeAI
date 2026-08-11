import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.bootstrap import bootstrap_founder_user
from app.config import get_settings
from app.db import SessionLocal, call_with_db_retry, migration_engine
from app.limiter import limiter
from app.providers.base import looks_like_placeholder_secret
from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges, apply_rls
from app.routers import (
    account,
    admin,
    agents,
    auth,
    chat,
    conversations,
    documents,
    health,
    knowledge,
    library,
    mainai_execution,
    mainai_jobs,
    memory,
    projects,
    workbench,
)
from app.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(title="LifeOS / MainAI API", version="0.1.0")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CSRF verification is folded into app/deps.py's get_current_user (every mutating route
# requires it) plus an explicit check in /refresh and /logout, which authenticate via the
# refresh-token cookie instead — not a standalone middleware, because the CSRF value can no
# longer live in a cookie the frontend can read (frontend/backend are different origins;
# see docs/AUTH_THREAT_MODEL.md) and verifying it now requires the same DB lookup those
# dependencies already do.

# allow_credentials=True is required for cookie-based auth to work cross-origin at all —
# combined with an explicit origin allow-list (never "*", which the Fetch spec forbids
# alongside credentials anyway) and explicit methods/headers. See docs/AUTH_THREAT_MODEL.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(account.router)
app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(documents.router)
app.include_router(projects.router)
app.include_router(knowledge.router)
app.include_router(library.router)
app.include_router(workbench.router)
app.include_router(admin.router)
app.include_router(memory.router)
app.include_router(agents.router)
app.include_router(mainai_jobs.router)
app.include_router(mainai_execution.router)


@app.on_event("startup")
def on_startup():
    # Schema is owned by Alembic migrations now (backend/alembic/versions/), applied as an
    # explicit deploy step (`alembic upgrade head` — see backend/docker-entrypoint.sh and
    # docs/OPERATIONS.md), never by the app itself at request-serving startup. RLS
    # enable/policy statements are also defined in the migrations, but apply_rls() is kept
    # here too as an idempotent safety net (cheap no-op if already applied) — see app/rls.py.
    #
    # Both DB touches below go through call_with_db_retry (app/db.py): verified production
    # incident, 2026-07-20 — this was the ONE unprotected first-ever connection to
    # APP_DATABASE_URL in the whole boot sequence, and a brief Supabase Session Pooler
    # auth-cache propagation lag right after backend/scripts/security/ensure_app_role.py provisioned
    # the role killed the entire process here ("Application startup failed. Exiting."), even
    # though the exact same credential worked moments later. ensure_app_role.py now also
    # retries its own self-test connection, so this is defense in depth, not the only guard.
    call_with_db_retry(lambda: apply_rls(migration_engine))
    # See app/rls.py's own docstring: mainai_app's blanket ALL PRIVILEGES grant is
    # unconditionally re-applied by ensure_app_role.py on every boot, BEFORE this code runs —
    # a REVOKE from migration 0026 alone would be silently undone by the next restart without
    # this every-boot reassertion (the exact Pass 12 boot-persistence bug class, applied here).
    call_with_db_retry(lambda: apply_mainai_job_runtime_privileges(migration_engine))
    # Same every-boot reassertion, for the MainAI Execution Loop V0.1 objects (migration 0032)
    # — see app/rls.py's apply_mainai_execution_privileges() docstring.
    call_with_db_retry(lambda: apply_mainai_execution_privileges(migration_engine))

    _check_smtp_mode()
    _warn_placeholder_provider_keys()

    if settings.environment == "production":
        _check_smtp_configured()
        _check_no_placeholder_secrets()
        _check_cookies_secure()

    if settings.redis_url:
        _check_redis_reachable()

    def _bootstrap():
        db = SessionLocal()
        try:
            bootstrap_founder_user(db)
        finally:
            db.close()

    call_with_db_retry(_bootstrap)

    start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


def _check_smtp_mode() -> None:
    """STARTTLS (smtp_use_tls, plaintext-then-upgrade — typically port 587) and implicit
    TLS/SSL (smtp_use_ssl, TLS from the first byte — typically port 465, e.g. Strato's
    smtp.strato.com:465) are two different wire protocols selected by app/email.py's
    _send_via_smtp(); a server only speaks one of them on a given port, so both flags true is
    a real misconfiguration, not a harmless redundancy. Fail at startup, not on the first
    delivery attempt.

    Both flags false is a separate misconfiguration: _send_via_smtp() then falls through to a
    plain, unencrypted smtplib.SMTP connection with no .starttls() call — real mail sent over
    the wire in cleartext. That's only rejected in production; non-production environments
    (dev/CI, often pointed at a throwaway/placeholder SMTP_HOST) are left exactly as before."""
    if not settings.smtp_host:
        return
    if settings.smtp_use_tls and settings.smtp_use_ssl:
        raise RuntimeError(
            "SMTP_USE_TLS och SMTP_USE_SSL är båda satta till true. De är ömsesidigt "
            "uteslutande anslutningslägen (STARTTLS respektive implicit TLS/SSL) — välj "
            "exakt ett beroende på vad SMTP-servern på SMTP_PORT faktiskt talar. "
            "T.ex. Strato: port 465 => SMTP_USE_SSL=true, SMTP_USE_TLS=false. "
            "Port 587 (de flesta andra) => SMTP_USE_TLS=true, SMTP_USE_SSL=false."
        )
    if settings.environment == "production" and not settings.smtp_use_tls and not settings.smtp_use_ssl:
        raise RuntimeError(
            "SMTP_HOST är satt men varken SMTP_USE_TLS eller SMTP_USE_SSL är true. I "
            "produktion skulle detta skicka e-post okrypterat i klartext. Sätt exakt en av "
            "dem till true beroende på vad SMTP-servern på SMTP_PORT faktiskt talar."
        )


def _check_smtp_configured() -> None:
    """SMTP is mandatory in production: without it, app/email.py's dev-only fallback just
    logs a warning and drops the message (verification/reset links carry a one-time
    credential that must never end up in the regular application log — see app/email.py and
    docs/AUTH_THREAT_MODEL.md), so registration and password reset would silently never
    reach anyone. Fail at startup, not on the first real user's request."""
    if not settings.smtp_host:
        raise RuntimeError(
            "SMTP_HOST är inte satt i en produktionsmiljö (ENVIRONMENT=production). "
            "E-postverifiering och lösenordsåterställning kräver en fungerande SMTP-server "
            "— sätt SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD (se .env.example)."
        )


def _redact_url_credentials(url: str) -> str:
    """Returns `url` with any userinfo password replaced by `***`, for safe inclusion in a
    startup error message. Startup exceptions routinely end up in the regular application
    log / Render's log stream (far broader read-access and longer retention than a secret
    manager) — see app/email.py and app/routers/health.py, which already deliberately never
    echo connection strings or exception details past a generic message for exactly this
    reason. Falls back to returning the input unchanged if it doesn't parse as a URL at all,
    rather than raising a second, more confusing error out of an error handler."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.password:
        return url
    userinfo = f"{parts.username}:***" if parts.username else ":***"
    netloc = f"{userinfo}@{parts.hostname or ''}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _check_redis_reachable() -> None:
    """Fail fast, not silently: if REDIS_URL is set, rate limiting is expected to actually
    be distributed (see app/limiter.py) — an unreachable Redis at startup should stop the
    app from serving traffic under a false sense of protection, rather than only surfacing
    as a mysterious error on the first rate-limited request."""
    import redis

    client = redis.from_url(settings.redis_url, socket_connect_timeout=5)
    try:
        client.ping()
    except redis.RedisError as exc:
        raise RuntimeError(
            f"REDIS_URL är satt ({_redact_url_credentials(settings.redis_url)}) men Redis "
            "kunde inte nås vid uppstart. Rate limiting kräver en fungerande delad lagring i "
            "den här konfigurationen — kontrollera att Redis-tjänsten kör innan backend "
            "startas."
        ) from exc
    finally:
        client.close()


def _warn_placeholder_provider_keys() -> None:
    """A provider API key that's non-empty but obviously a leftover template placeholder
    (see app/providers/base.py's looks_like_placeholder_secret — real incident, 2026-07-26: a
    duplicated env-var line left one active) is silently treated as "not configured" by every
    provider's is_configured(), so it can never crash a request — but that silence means a
    founder who thinks they configured a provider gets no signal that they didn't. Runs in
    every environment (unlike _check_no_placeholder_secrets, which only runs in production
    and hard-fails): a placeholder AI provider key is never a startup-blocking problem — chat
    already degrades cleanly with zero providers configured at all — so this only warns,
    loudly, in the log, never raises. Never logs the value itself, only which field."""
    placeholder_fields = {
        "OPENAI_API_KEY": settings.openai_api_key,
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "GOOGLE_API_KEY": settings.google_api_key,
        "DEEPSEEK_API_KEY": settings.deepseek_api_key,
        "OPENROUTER_API_KEY": settings.openrouter_api_key,
    }
    for name, value in placeholder_fields.items():
        if looks_like_placeholder_secret(value):
            logger.warning(
                "%s is set to what looks like a leftover template placeholder value, not a "
                "real credential — treated as not configured (this provider will be skipped, "
                "never attempted with a bad key). Set a real key or remove the variable "
                "entirely.",
                name,
            )


def _check_no_placeholder_secrets() -> None:
    """Every one of these has an insecure, publicly-readable-in-this-repo default value —
    fine for local dev (see docker-compose.yml), but if any of them is still that default
    once ENVIRONMENT=production, the deployment is either misconfigured (an operator forgot
    to set it in the Render dashboard) or, worse, an attacker who has read this file already
    knows a working credential for the single account with full MainAI access. Fail loudly
    at startup rather than silently accepting a knowable password."""
    placeholder_fields = {
        "SECRET_KEY": settings.secret_key == "change-me-in-production",
        "FOUNDER_PASSWORD": settings.founder_password == "change-me-in-production",
        "FOUNDER_EMAIL": settings.founder_email == "founder@lifeos.local",
    }
    still_default = [name for name, is_placeholder in placeholder_fields.items() if is_placeholder]
    if still_default:
        raise RuntimeError(
            f"Följande miljövariabler är fortfarande satta till sina osäkra/oanvändbara "
            f"standardvärden i en produktionsmiljö (ENVIRONMENT=production): "
            f"{', '.join(still_default)}. Sätt riktiga värden i Render-dashboarden "
            "(sync: false i render.yaml — aldrig committade) innan produktionsdeploy."
        )


def _check_cookies_secure() -> None:
    """COOKIE_SECURE=false in production would let the session cookies (which are what
    require_founder() ultimately trusts) be sent over plain HTTP — trivially interceptable
    on any network path that isn't fully HTTPS end-to-end. See docs/AUTH_THREAT_MODEL.md.
    There's no legitimate production configuration where this should be false; the default
    (true) already matches — this only fires if something explicitly overrode it."""
    if not settings.cookie_secure:
        raise RuntimeError(
            "COOKIE_SECURE är satt till false i en produktionsmiljö (ENVIRONMENT=production). "
            "Sessionskakorna skulle då kunna skickas okrypterat — sätt COOKIE_SECURE=true "
            "(standardvärdet) eller ta bort variabeln helt så standardvärdet gäller."
        )
