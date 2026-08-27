"""SQLAlchemy model for migration 0062 -- the Life Vault / External-AI Egress disclosure
ledger. See docs/LIFE_VAULT_EGRESS_CONTROL.md for the full threat model this table exists to
make auditable."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProviderDisclosureDecision(str, enum.Enum):
    allowed = "allowed"
    denied = "denied"


class ProviderDisclosureEvent(Base):
    """One egress-policy decision -- allowed or denied -- for one attempted external-provider
    call. Never stores raw content, only hashes (see migration 0062's own docstring). Append-
    only at the database level."""

    __tablename__ = "provider_disclosure_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(128))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    goal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    redaction_categories: Mapped[list] = mapped_column(JSONB, default=list)
    attempted_content_hash: Mapped[str] = mapped_column(String(64))
    sent_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
