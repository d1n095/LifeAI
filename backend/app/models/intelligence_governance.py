"""Deterministic evidence foundation for agent evaluation and meta-learning.

These records describe work performed through the existing MainAI runtime.  They do not
dispatch work, rank providers, or replace MainAI task/job/event truth.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExecutionRole(str, enum.Enum):
    builder = "builder"
    reviewer = "reviewer"
    challenger = "challenger"
    verifier = "verifier"
    planner = "planner"
    unknown = "unknown"


class ParticipationMode(str, enum.Enum):
    primary = "primary"
    shadow = "shadow"
    parallel = "parallel"
    challenger = "challenger"
    reviewer = "reviewer"
    unknown = "unknown"


class ClassificationBasis(str, enum.Enum):
    deterministic = "deterministic"
    manual = "manual"
    inferred = "inferred"
    unknown = "unknown"


class ReviewKind(str, enum.Enum):
    self_review = "self_review"
    independent_model = "independent_model"
    deterministic_tool = "deterministic_tool"
    founder = "founder"
    unknown = "unknown"


class IdeaDisposition(str, enum.Enum):
    accepted = "accepted"
    rejected = "rejected"
    deferred = "deferred"
    unknown = "unknown"


class IntelligenceExecution(Base):
    """Immutable identity/context snapshot for one candidate execution of a MainAI task."""

    __tablename__ = "intelligence_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_tasks.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_identity: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_environment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    available_tools: Mapped[list] = mapped_column(JSON, default=list)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    work_strategy_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(24), default=ExecutionRole.unknown.value)
    participation_mode: Mapped[str] = mapped_column(String(24), default=ParticipationMode.unknown.value)
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    classification_basis: Mapped[str] = mapped_column(String(24), default=ClassificationBasis.unknown.value)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IntelligenceEvidence(Base):
    """Append-only raw observation. Derived judgments belong in IntelligenceInterpretation."""

    __tablename__ = "intelligence_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_executions.id", ondelete="CASCADE"), index=True)
    observer_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_executions.id", ondelete="SET NULL"), nullable=True)
    task_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_task_events.id", ondelete="SET NULL"), nullable=True)
    recovery_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_recovery_records.id", ondelete="SET NULL"), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(48))
    review_kind: Mapped[str] = mapped_column(String(32), default=ReviewKind.unknown.value)
    deterministic: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON)
    source_type: Mapped[str] = mapped_column(String(48))
    source_ref: Mapped[str] = mapped_column(String(256))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IntelligenceInterpretation(Base):
    """Append-only derived interpretation; recalculation inserts a new row."""

    __tablename__ = "intelligence_interpretations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_evidence.id", ondelete="CASCADE"), index=True)
    interpretation_kind: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON)
    method: Mapped[str] = mapped_column(String(128))
    classification_basis: Mapped[str] = mapped_column(String(24))
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_interpretations.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class IntelligenceIdea(Base):
    """One preserved idea or assumption, independent of its execution's overall result."""

    __tablename__ = "intelligence_ideas"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_executions.id", ondelete="CASCADE"), index=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_evidence.id", ondelete="SET NULL"), nullable=True)
    idea_kind: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(String(24), default=IdeaDisposition.unknown.value)
    disposition_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_basis: Mapped[str] = mapped_column(String(24), default=ClassificationBasis.unknown.value)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class IntelligenceIdeaLink(Base):
    """Narrow idea relationship, not a generic knowledge graph."""

    __tablename__ = "intelligence_idea_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_idea_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_ideas.id", ondelete="CASCADE"))
    to_idea_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_ideas.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(32))
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_evidence.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class IntelligenceIdeaLesson(Base):
    __tablename__ = "intelligence_idea_lessons"

    idea_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("intelligence_ideas.id", ondelete="CASCADE"), primary_key=True)
    engineering_lesson_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("engineering_lessons.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
