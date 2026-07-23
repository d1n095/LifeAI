import logging
import time
from typing import Callable, TypeVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings
from app.request_context import current_user_id

logger = logging.getLogger("mainai.db")
_T = TypeVar("_T")

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


def call_with_db_retry(fn: Callable[[], _T], *, attempts: int = 5, base_delay_seconds: float = 1.0) -> _T:
    """Retries fn() with exponential backoff (1s/2s/4s/8s by default) on a transient DB
    connection failure. Verified production incident, 2026-07-20: app/main.py's on_startup()
    made its first-ever mainai_app connection with no retry at all, so a brief Supabase
    Session Pooler auth-cache propagation lag right after ensure_app_role.py provisioned or
    changed the role's password (see that script's module docstring) killed the whole
    process — "ERROR: Application startup failed. Exiting." — even though the exact same
    credential worked fine moments later. Used only around DB-touching startup code, not
    request handling (a request-scoped failure should surface to that one caller immediately,
    not silently retry for several seconds behind the scenes). Re-raises after the last
    attempt so a genuinely broken connection still fails startup loudly, as before."""
    last_exc: OperationalError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OperationalError as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "DB-anslutning misslyckades vid uppstart (försök %d/%d): %s — försöker igen om %.1fs.",
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
