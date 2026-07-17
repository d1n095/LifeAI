from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings
from app.request_context import current_user_id

settings = get_settings()

# Superuser connection — schema migrations and RLS setup ONLY (see config.py for why these
# must stay separate from the runtime connection).
migration_engine = create_engine(settings.database_url, pool_pre_ping=True)

# Restricted, non-superuser connection — used for every request. This is what makes RLS real.
engine = create_engine(settings.app_database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(SessionLocal, "after_begin")
def _bind_rls_user(session, transaction, connection):
    """Re-apply the RLS session variable on every new transaction, not just the first.

    `SET LOCAL` only lasts for one transaction — a mid-request `db.commit()` (e.g. after
    creating a row, followed by `db.refresh()`) starts a new transaction that would
    otherwise run without it. The user id itself lives in a contextvar set once per
    request in app/deps.py, so this works regardless of which pooled connection is used.
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
