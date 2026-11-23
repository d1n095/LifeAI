"""Founder email-change request — the durable half of the secure founder account recovery.

One outstanding request per founder at a time: issuing a new request invalidates any
previous unused one (same convention as EmailVerificationToken). Only the SHA-256 hash of
the verification token is stored — the plaintext exists only in the emailed link and is
never persisted or logged (see app/security.py). The OLD email is kept on the row purely
for the audit trail of the request record; the completed change itself is audited without
email addresses (app/founder_recovery.py).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FounderEmailChangeRequest(Base):
    __tablename__ = "founder_email_change_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    # Verified, confirmed NEW address — not written to users.email until the emailed link
    # for THIS row is redeemed (single-use).
    new_email: Mapped[str] = mapped_column(String(320))
    # Address at initiate time, kept only on this request row for the audit trail.
    old_email: Mapped[str] = mapped_column(String(320))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
