from sqlalchemy.orm import Session

from app.config import get_settings
from app.email_utils import normalize_email
from app.models.user import User, UserRole
from app.security import hash_password, utcnow_seconds_baseline


def bootstrap_admin_user(db: Session) -> None:
    """Create the first admin account from ADMIN_EMAIL/ADMIN_PASSWORD if no users exist yet.

    Runs once — after the first user is created (by anyone), this is a no-op forever.

    Pre-verified by construction: this account is provisioned by whoever deploys the app
    (via environment variables), not through the public self-registration flow, so there is
    no email to click a verification link from — requiring one would make the bootstrap
    account permanently unusable.
    """
    if db.query(User).first() is not None:
        return

    settings = get_settings()
    now = utcnow_seconds_baseline()  # must match JWT iat precision — see app/security.py
    admin = User(
        email=normalize_email(settings.admin_email),
        password_hash=hash_password(settings.admin_password),
        role=UserRole.admin,
        email_verified=True,
        email_verified_at=now,
        sessions_valid_after=now,
    )
    db.add(admin)
    db.commit()
