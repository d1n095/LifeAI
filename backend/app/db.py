from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings
from app.request_context import current_user_id

settings = get_settings()

# Superuser connection — schema migrations and RLS setup ONLY (see config.py for why these
# must stay separate from the runtime connection).
migration_engine = create_engine(settings.database_url, pool_pre_ping=True)

# Restricted, non-superuser connection — used for every request. This is what makes RLS real.
#
# expire_on_commit=False is deliberate, not a default left in place: with the default
# (True), every attribute on an object becomes "expired" after db.commit() — not just ones
# touched by an explicit db.refresh(). The *next* attribute access (e.g. `conversation.id`
# to log a message) then silently opens a brand-new transaction to reload it. That new
# transaction begins before our RLS session variable is guaranteed to be re-applied to it,
# so a route that commits and then keeps using the same object could intermittently lose
# its RLS context mid-request. Turning off auto-expiry means committed objects keep serving
# their already-known values from memory, which is exactly what a short-lived,
# request-scoped session should do anyway.
engine = create_engine(settings.app_database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


@event.listens_for(SessionLocal, "after_begin")
def _bind_rls_user(session, transaction, connection):
    """Re-apply the RLS session variable on every new transaction, not just the first.

    Defense in depth on top of expire_on_commit=False above: covers any future code path
    that runs a genuinely new query (not just an attribute refresh) after a mid-request
    commit. The user id lives in a contextvar set once per request in app/deps.py, so this
    works regardless of which pooled connection is used.
    """
    user_id = current_user_id.get()
    if user_id:
        connection.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": user_id})


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
