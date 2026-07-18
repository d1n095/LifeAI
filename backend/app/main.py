from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.bootstrap import bootstrap_admin_user
from app.config import get_settings
from app.db import Base, SessionLocal, migration_engine
from app.limiter import limiter
from app.rls import apply_rls
from app.routers import account, admin, auth, chat, conversations, documents, health, knowledge, projects

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
    # MVP: create tables directly. Replace with Alembic migrations before production (see ROADMAP Fas 1).
    # Both run through the superuser/migration connection — see app/db.py for why.
    Base.metadata.create_all(bind=migration_engine)
    apply_rls(migration_engine)

    db = SessionLocal()
    try:
        bootstrap_admin_user(db)
    finally:
        db.close()
