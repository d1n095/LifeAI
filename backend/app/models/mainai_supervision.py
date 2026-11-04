from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAISupervisionAgent(Base):
    __tablename__ = "mainai_supervision_agents"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    process_nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    progress_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MainAISupervisionMessage(Base):
    __tablename__ = "mainai_supervision_messages"
    __table_args__ = (UniqueConstraint("owner_id", "job_id", "kind", "sequence", name="uq_mainai_supervision_message"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MainAIBudgetReservation(Base):
    __tablename__ = "mainai_budget_reservations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), index=True)
    amount: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MainAISupervisionTelemetry(Base):
    """Privacy-safe resource observations for the future scheduler.

    These are observations, never authority.  Missing provider values remain
    NULL rather than being guessed, and each row is owner-scoped and time-bound.
    """

    __tablename__ = "mainai_supervision_telemetry"
    __table_args__ = (UniqueConstraint("owner_id", "agent_id", "observed_at", name="uq_supervision_telemetry_sample"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="SET NULL"), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    productive_seconds: Mapped[float | None] = mapped_column(Numeric(18, 3), nullable=True)
    idle_seconds: Mapped[float | None] = mapped_column(Numeric(18, 3), nullable=True)
    blocked_seconds: Mapped[float | None] = mapped_column(Numeric(18, 3), nullable=True)
    stalled_seconds: Mapped[float | None] = mapped_column(Numeric(18, 3), nullable=True)
    continuation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    premature_return_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    restart_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_limit_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_quota_remaining: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    reported_cost: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    validated_cost: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rework_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    examiner_outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handoff_ready: Mapped[bool] = mapped_column(nullable=False, default=False)
    context_risk: Mapped[str | None] = mapped_column(String(24), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
