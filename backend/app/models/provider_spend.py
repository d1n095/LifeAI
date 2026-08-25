"""SQLAlchemy models for migration 0060 provider-spend authorization."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProviderSpendAuthorizationStatus(str, enum.Enum):
    active = "active"
    superseded = "superseded"
    exhausted = "exhausted"
    expired = "expired"
    revoked = "revoked"


class ProviderSpendAuthorization(Base):
    """One founder-granted, ceiling-bounded provider-planning budget for one (goal, envelope).

    Never grants repo paths, capabilities, remote_write, or push — only billed planning spend.
    """

    __tablename__ = "provider_spend_authorizations"
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_provider_spend_authorizations_idem"),
        UniqueConstraint("id", "owner_id", name="uq_provider_spend_authorizations_id_owner"),
        ForeignKeyConstraint(
            ["goal_id", "owner_id"],
            ["mainai_goals.id", "mainai_goals.owner_id"],
            name="fk_provider_spend_authorizations_goal_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["execution_envelope_id", "owner_id"],
            ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"],
            name="fk_provider_spend_authorizations_envelope_owner",
            ondelete="CASCADE",
        ),
        # Column list authored in migration 0060 — SQLAlchemy cannot express it; keep in sync.
        ForeignKeyConstraint(
            ["supersedes_authorization_id", "owner_id"],
            ["provider_spend_authorizations.id", "provider_spend_authorizations.owner_id"],
            name="fk_provider_spend_authorizations_supersedes",
            ondelete="SET NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    execution_envelope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    authorized_by: Mapped[str] = mapped_column(String(64))
    authorized_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(24), default=ProviderSpendAuthorizationStatus.active.value)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    max_requests: Mapped[int] = mapped_column(Integer)
    max_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allowed_providers: Mapped[list] = mapped_column(JSONB, default=list)
    allowed_models: Mapped[list] = mapped_column(JSONB, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    spent_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    spent_requests: Mapped[int] = mapped_column(Integer, default=0)
    spent_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    spent_completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    supersedes_authorization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProviderSpendUsageEvent(Base):
    """Append-only spend observation. Idempotent on source_ref so retries cannot inflate spend."""

    __tablename__ = "provider_spend_usage_events"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_provider_spend_usage_events_source_ref"),
        ForeignKeyConstraint(
            ["authorization_id", "owner_id"],
            ["provider_spend_authorizations.id", "provider_spend_authorizations.owner_id"],
            name="fk_provider_spend_usage_events_auth_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "owner_id"],
            ["mainai_goals.id", "mainai_goals.owner_id"],
            name="fk_provider_spend_usage_events_goal_owner",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    authorization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"))
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_ref: Mapped[str] = mapped_column(String(320))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
