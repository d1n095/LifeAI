from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RevokedAccessToken(Base):
    """Short-lived blocklist keyed by JWT `jti`. Without this, logout/family-revocation
    could only stop a session from being *renewed* — the access token itself, being a
    self-contained stateless JWT, would keep working for up to its own natural expiry
    (access_token_expire_minutes). This table is what makes logout actually immediate.

    Deliberately tiny and self-pruning (see app/security.py revoke_access_token) rather than
    a general-purpose table: entries are only ever as long-lived as the access token they
    block, so it never grows unbounded.
    """

    __tablename__ = "revoked_access_tokens"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
