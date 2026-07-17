from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.user import User, UserRole
from app.security import hash_password


def bootstrap_admin_user(db: Session) -> None:
    """Create the first admin account from ADMIN_EMAIL/ADMIN_PASSWORD if no users exist yet.

    Runs once — after the first user is created (by anyone), this is a no-op forever.
    """
    if db.query(User).first() is not None:
        return

    settings = get_settings()
    admin = User(
        email=settings.admin_email.lower(),
        password_hash=hash_password(settings.admin_password),
        role=UserRole.admin,
    )
    db.add(admin)
    db.commit()
