import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.email_utils import normalize_email
from app.founder import FOUNDER_USER_ID
from app.founder_memory.service import record_founder_memory
from app.models.user import User, UserRole
from app.security import hash_password, utcnow_seconds_baseline

logger = logging.getLogger(__name__)

# The founder's locked priority, registered once at bootstrap so it can never live only in a
# conversation. `note_type="decision"`, `authority="founder"`, `basis="manual"` are the
# schema's own closed vocabulary (migration 0049); nothing is inferred here.
LOCKED_PRIORITY = "Prioritering (låst): Founder Alpha först, utökad autonomi senare."
LOCKED_PRIORITY_KEY = "locked-priority:founder-alpha-first"


def bootstrap_founder_user(db: Session) -> None:
    """Create the single, permanent MainAI founder account from FOUNDER_EMAIL/FOUNDER_PASSWORD
    if it doesn't exist yet.

    Idempotent on FOUNDER_USER_ID specifically (not "any user exists"): that fixed id is the
    actual source of truth app/deps.py's require_founder() checks against, so this is the
    correct existence check even if other (unreachable — see register()'s production block)
    rows exist in the table for any reason.

    Pre-verified by construction, same as the account it replaces: this account is
    provisioned by whoever deploys the app (via environment variables in the Render
    dashboard), not through the public self-registration flow, so there is no email to click
    a verification link from — requiring one would make the account permanently unusable.
    """
    if db.get(User, FOUNDER_USER_ID) is not None:
        return

    settings = get_settings()
    now = utcnow_seconds_baseline()  # must match JWT iat precision — see app/security.py
    founder = User(
        id=FOUNDER_USER_ID,
        email=normalize_email(settings.founder_email),
        password_hash=hash_password(settings.founder_password),
        role=UserRole.founder,
        email_verified=True,
        email_verified_at=now,
        sessions_valid_after=now,
    )
    db.add(founder)
    db.commit()


def bootstrap_founder_priorities(db: Session) -> None:
    """Registers the founder's locked priority once, as a durable founder DECISION.

    Idempotent by key (`founder_memory_notes` has a unique (owner_id, idempotency_key)).
    Runs under the SAME RLS identity the request middleware sets — without it, FORCE RLS
    silently turns the insert into a no-op, which is exactly the R14 finding this function
    fixes. Failures are logged rather than raised: a missing priority note must not stop the
    API from starting, but it must also never disappear without a trace.
    """
    founder = db.get(User, FOUNDER_USER_ID)
    if founder is None:
        return
    try:
        db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(founder.id)})
        record_founder_memory(
            db,
            owner_id=founder.id,
            note_type="decision",
            content=LOCKED_PRIORITY,
            idempotency_key=LOCKED_PRIORITY_KEY,
            authority="founder",
            basis="manual",
            source="bootstrap",
            provenance={"registered_by": "bootstrap_founder_priorities"},
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("could not register the locked founder priority (non-fatal)", exc_info=True)
