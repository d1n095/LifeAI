"""Deterministic, provider-independent evidence about how work was performed."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkStrategy(Base):
    __tablename__ = "work_strategies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    work_category: Mapped[str] = mapped_column(String(64), default="unknown")
    ordered_phases: Mapped[list] = mapped_column(JSON, default=list)
    tool_sequence: Mapped[list] = mapped_column(JSON, default=list)
    methods: Mapped[dict] = mapped_column(JSON, default=dict)
    environment_assumptions: Mapped[dict] = mapped_column(JSON, default=dict)
    classification_basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    predecessor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkStrategyExecution(Base):
    __tablename__ = "work_strategy_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategies.id", ondelete="CASCADE"),
        index=True,
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_executions.id", ondelete="CASCADE"),
        index=True,
    )
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("life_problems.id", ondelete="SET NULL"),
        nullable=True,
    )
    approach_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("life_problem_approaches.id", ondelete="SET NULL"),
        nullable=True,
    )
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    next_trace_sequence: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkTraceEvent(Base):
    __tablename__ = "work_trace_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategy_executions.id", ondelete="CASCADE"),
        index=True,
    )
    sequence_number: Mapped[int] = mapped_column(Integer)
    action_type: Mapped[str] = mapped_column(String(48))
    tool_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    target_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    action_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(32), default="unknown")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    items_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    usage_log_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usage_log.id", ondelete="SET NULL"),
        nullable=True,
    )
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkEfficiencyObservation(Base):
    __tablename__ = "work_efficiency_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategy_executions.id", ondelete="CASCADE"),
        index=True,
    )
    trace_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_trace_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    metric_type: Mapped[str] = mapped_column(String(48))
    numeric_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    unit: Mapped[str] = mapped_column(String(24))
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkStrategyFinding(Base):
    __tablename__ = "work_strategy_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategy_executions.id", ondelete="CASCADE"),
        index=True,
    )
    trace_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_trace_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    finding_type: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text)
    justified: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkVerificationObligation(Base):
    __tablename__ = "work_verification_obligations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategy_executions.id", ondelete="CASCADE"),
        index=True,
    )
    requirement_kind: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mainai_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkVerificationObservation(Base):
    __tablename__ = "work_verification_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_verification_obligations.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    reason: Mapped[str] = mapped_column(Text)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkStoppingDecision(Base):
    __tablename__ = "work_stopping_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategy_executions.id", ondelete="CASCADE"),
        index=True,
    )
    decision_type: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    subsequent_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkSpecialistContribution(Base):
    __tablename__ = "work_specialist_contributions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategy_executions.id", ondelete="CASCADE"),
        index=True,
    )
    specialist_execution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_executions.id", ondelete="CASCADE"),
        index=True,
    )
    purpose: Mapped[str] = mapped_column(Text)
    contribution: Mapped[str] = mapped_column(String(32), default="unknown")
    evidence_available_before: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkStrategyLessonLink(Base):
    __tablename__ = "work_strategy_lesson_links"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_strategies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    engineering_lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("engineering_lessons.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relation: Mapped[str] = mapped_column(String(32))
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intelligence_evidence.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
