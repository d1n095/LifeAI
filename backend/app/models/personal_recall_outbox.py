"""Canonical transactionally emitted routing events for Personal Recall."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RecallOutboxEventType(str, enum.Enum):
    created = "created"
    updated = "updated"
    deleted = "deleted"
    revoked = "revoked"
    purged = "purged"
    superseded = "superseded"
    restored = "restored"


class PersonalRecallOutbox(Base):
    __tablename__ = "personal_recall_outbox"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    canonical_version: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    transaction_id: Mapped[int] = mapped_column(BigInteger)
    routing_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
