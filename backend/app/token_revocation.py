from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.revoked_access_token import RevokedAccessToken

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
