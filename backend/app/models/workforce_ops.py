"""Additional workforce ops models (migration 0068)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, ForeignKeyConstraint, Integer, String, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkforceAssignmentCheckpoint(Base):
    """Durable partial progress for failure/takeover/resume (T13).

    UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT — retry/reassign refuses consequential
    re-execution when external_effect_state is unknown or effect_proven.
    """

    __tablename__ = "workforce_assignment_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    checkpoint_kind: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer, default=1)
    partial_result: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    external_effect_state: Mapped[str] = mapped_column(String(32), default="none_known")
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recoverable: Mapped[bool] = mapped_column(Boolean, default=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "owner_id"],
            ["workforce_assignments.id", "workforce_assignments.owner_id"],
            ondelete="CASCADE",
        ),
    )


class WorkforceLifecycleEvent(Base):
    """Hiring / learning lifecycle audit (T9/T10). trained=True only for real fine-tunes."""

    __tablename__ = "workforce_lifecycle_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    change_kind: Mapped[str] = mapped_column(String(64))
    change_summary: Mapped[str] = mapped_column(Text)
    evidence_before: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_after: Mapped[dict] = mapped_column(JSON, default=dict)
    rollback_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    trained: Mapped[bool] = mapped_column(Boolean, default=False)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["profile_id", "owner_id"],
            ["workforce_agent_profiles.id", "workforce_agent_profiles.owner_id"],
            ondelete="CASCADE",
        ),
    )


class WorkforceCostBudget(Base):
    """Organizational cost ceilings (T16). Real provider calls still use app.provider_spend."""

    __tablename__ = "workforce_cost_budgets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope_kind: Mapped[str] = mapped_column(String(32))
    scope_ref: Mapped[str] = mapped_column(String(128))
    cap_usd: Mapped[float] = mapped_column(Float)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    reserved_usd: Mapped[float] = mapped_column(Float, default=0.0)
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkforceVerificationDecision(Base):
    """Append-ish verification decision history (T14)."""

    __tablename__ = "workforce_verification_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    policy_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    verifier_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    second_verifier_profile_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    test_evidence_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    deterministic_validator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    founder_approval_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    agreement: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "owner_id"],
            ["workforce_assignments.id", "workforce_assignments.owner_id"],
            ondelete="CASCADE",
        ),
    )
