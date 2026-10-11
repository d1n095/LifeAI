import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.refresh_token import RefreshToken
from app.models.revoked_access_token import RevokedAccessToken
from app.security import utcnow_seconds

settings = get_settings()


def revoke_access_token(db: Session, jti: str | None) -> None:
    """Blocklist one access token by jti so it stops working immediately, instead of only
    being prevented from renewing. Also opportunistically prunes expired blocklist entries
    so this table never grows unbounded — every entry is naturally worthless once the
    token it blocks would have expired anyway."""
    if jti is None:
        return
    db.query(RevokedAccessToken).filter(RevokedAccessToken.expires_at < datetime.utcnow()).delete()
    if db.get(RevokedAccessToken, jti) is None:
        db.add(RevokedAccessToken(jti=jti, expires_at=datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)))
    db.commit()


def is_access_token_revoked(db: Session, jti: str | None) -> bool:
    if jti is None:
        return False
    return db.get(RevokedAccessToken, jti) is not None


def revoke_all_sessions_for_user(db: Session, user_id: uuid.UUID, *, bump_epoch: bool = True) -> None:
    """Invalidates every session a user has, on every device, at once: bumps
    sessions_valid_after (kills every outstanding access token instantly via the iat check
    in app/deps.py, without enumerating and blocklisting each jti individually) and revokes
    every still-active refresh-token row across every family (so no session can silently
    continue via refresh either). Used by both the explicit "log out everywhere" endpoint
    and password reset — a reset that left old sessions alive would defeat the point of
    resetting a possibly-compromised password."""
    now = utcnow_seconds()  # must match JWT iat precision — see app/security.py
    if bump_epoch:
        current = db.execute(
            text("SELECT NULLIF(current_setting('app.current_user_id', true), '')")
        ).scalar()
        if current == str(user_id):
            db.execute(text("SELECT bump_sessions_valid_after(:uid)"), {"uid": str(user_id)})
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).update(
        {"revoked_at": now}
    )
    db.commit()
