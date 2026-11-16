import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WebAuthnCredential(Base):
    """One registered passkey for a Founder/user account. RLS owner-scoped (see migration
    webauthn_passkeys_01 + app/rls.py). The public key and raw credential id are stored; the
    private key never leaves the platform authenticator (Touch ID / Android fingerprint)."""

    __tablename__ = "webauthn_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # Signature counter: must only ever increase (a decrease means a cloned/replayed
    # authenticator) — enforced at verification time in app/webauthn_service.py.
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0)
    transports: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    aaguid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    backup_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_state: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebAuthnChallenge(Base):
    """A pending WebAuthn challenge, keyed by an unguessable random handle returned to the
    client and looked up on completion. Deliberately NOT RLS-scoped: the authentication
    ceremony runs before login (no app.current_user_id). Single-use (consumed_at) and
    short-lived (expires_at); holds only a random challenge, so it carries no sensitive data."""

    __tablename__ = "webauthn_challenges"

    handle: Mapped[str] = mapped_column(String(64), primary_key=True)
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    ceremony: Mapped[str] = mapped_column(String(16))  # 'register' | 'authenticate'
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
