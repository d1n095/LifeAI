import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RefreshToken(Base):
    """One row per issued refresh token. Never stores the token itself, only its hash —
    same principle as password storage, simpler mechanism since the value is already
    high-entropy random (see app/security.py).

    `family_id` ties together an entire rotation chain: every refresh replaces the current
    row with a new one sharing the same family_id. Reusing an already-revoked token (a
    replay of a stolen/superseded token) revokes the whole family, not just that row —
    see app/routers/auth.py and docs/AUTH_THREAT_MODEL.md.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex digest
    # jti of the access token minted alongside this refresh token — lets logout/family
    # revocation blocklist that specific access token too (app/models/revoked_access_token.py)
    # instead of only preventing renewal and waiting out its natural expiry.
    access_jti: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Plaintext by design, unlike token_hash above: this value grants no access on its own
    # (it only defeats cross-origin forgery, see docs/AUTH_THREAT_MODEL.md) and the frontend
    # must be able to receive it back to echo as a header, so hashing it at rest would add
    # complexity without reducing real risk — a DB compromise exposing it doesn't help an
    # attacker who doesn't also already have the HttpOnly session cookies.
    csrf_token: Mapped[str] = mapped_column(String(64))
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
