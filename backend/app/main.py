from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.bootstrap import bootstrap_admin_user
from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.limiter import limiter
from app.rls import apply_rls
from app.routers import account, admin, auth, chat, conversations, documents, health, knowledge, projects
from app.scheduler import start_scheduler, stop_scheduler

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
app.include_router(admin.router)


@app.on_event("startup")
def on_startup():
    # Schema is owned by Alembic migrations now (backend/alembic/versions/), applied as an
    # explicit deploy step (`alembic upgrade head` — see backend/docker-entrypoint.sh and
    # docs/OPERATIONS.md), never by the app itself at request-serving startup. RLS
    # enable/policy statements are also defined in the migrations, but apply_rls() is kept
    # here too as an idempotent safety net (cheap no-op if already applied) — see app/rls.py.
    apply_rls(migration_engine)

    if settings.redis_url:
        _check_redis_reachable()

    db = SessionLocal()
    try:
        bootstrap_admin_user(db)
    finally:
        db.close()

    start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


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
            f"REDIS_URL är satt ({settings.redis_url}) men Redis kunde inte nås vid uppstart. "
            "Rate limiting kräver en fungerande delad lagring i den här konfigurationen — "
            "kontrollera att Redis-tjänsten kör innan backend startas."
        ) from exc
    finally:
        client.close()
