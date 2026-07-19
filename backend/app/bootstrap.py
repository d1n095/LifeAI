from sqlalchemy.orm import Session

from app.config import get_settings
from app.email_utils import normalize_email
from app.founder import FOUNDER_USER_ID
from app.models.user import User, UserRole
from app.security import hash_password, utcnow_seconds_baseline


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
