"""Durable RT3 operating-cycle and authenticated provider-event state.

These rows are coordination state, never authority. Every transition re-reads the
existing execution envelope, workforce assignment and STOP epoch.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuthorizedOperatingCycle(Base):
    __tablename__ = "authorized_operating_cycles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    execution_envelope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    verifier_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    workforce_assignment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="cursor")
    external_agent_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    authority_epoch: Mapped[int] = mapped_column(Integer)
    global_authority_epoch: Mapped[int] = mapped_column(Integer)
    candidate_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_authorized_cycle_id_owner"),
        ForeignKeyConstraint(["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["execution_envelope_id", "owner_id"], ["execution_authorization_envelopes.id", "execution_authorization_envelopes.owner_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["current_task_id", "owner_id"], ["mainai_tasks.id", "mainai_tasks.owner_id"], ondelete="SET NULL"),
        ForeignKeyConstraint(["workforce_assignment_id", "owner_id"], ["workforce_assignments.id", "workforce_assignments.owner_id"], ondelete="SET NULL"),
        UniqueConstraint("owner_id", "goal_id", "execution_envelope_id", name="uq_authorized_cycle_envelope"),
        UniqueConstraint("provider", "external_agent_id", name="uq_authorized_cycle_provider_agent"),
    )


class ProviderWebhookEvent(Base):
    __tablename__ = "provider_webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32))
    event_key: Mapped[str] = mapped_column(String(128))
    body_sha256: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    external_agent_id: Mapped[str] = mapped_column(String(256), index=True)
    provider_status: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processing_state: Mapped[str] = mapped_column(String(32), default="received")
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("provider", "event_key", name="uq_provider_webhook_event_key"),
        ForeignKeyConstraint(["cycle_id", "owner_id"], ["authorized_operating_cycles.id", "authorized_operating_cycles.owner_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["assignment_id", "owner_id"], ["workforce_assignments.id", "workforce_assignments.owner_id"], ondelete="CASCADE"),
    )
