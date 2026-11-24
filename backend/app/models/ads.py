"""Advertising subsystem models (migration 0071). Isolated by design:

* No conversation/memory/agent-context columns exist — and none may be added here
  (tests enforce the firewall). The ad layer sees coarse context ONLY, via
  app/ads/context.py's whitelisted AdContext.
* Consent is an event trail (latest row decides) for demonstrability.
* ad_events carry pseudonymous session hashes, never emails or content.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AdDecision(str, enum.Enum):
    granted = "granted"
    denied = "denied"
    withdrawn = "withdrawn"


class AdConsentEvent(Base):
    __tablename__ = "ad_consent_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    decision: Mapped[AdDecision] = mapped_column(String(16))
    personalized: Mapped[bool] = mapped_column(Boolean, default=False)
    region: Mapped[str | None] = mapped_column(String(8), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(32))
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdEventType(str, enum.Enum):
    impression = "impression"
    click = "click"


class AdEvent(Base):
    __tablename__ = "ad_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    placement: Mapped[str] = mapped_column(String(64))
    event: Mapped[AdEventType] = mapped_column(String(16))
    creative_id: Mapped[str] = mapped_column(String(128))
    session_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
