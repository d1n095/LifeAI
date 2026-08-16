"""Deterministic comparison, experiment, and governed promotion evidence."""

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


class StrategyComparison(Base):
    __tablename__ = "strategy_comparisons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    baseline_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategy_executions.id"), index=True
    )
    challenger_binding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategy_executions.id"), index=True
    )
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_problems.id"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_tasks.id"), nullable=True
    )
    task_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(24), default="unknown")
    comparison_basis: Mapped[str] = mapped_column(String(24), default="unknown")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyComparabilityAssessment(Base):
    __tablename__ = "strategy_comparability_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    dimensions: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparability_assessments.id"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyQualityAssessment(Base):
    __tablename__ = "strategy_quality_assessments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        index=True,
    )
    subject: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32), default="unknown")
    required_count: Mapped[int] = mapped_column(Integer)
    passed_count: Mapped[int] = mapped_column(Integer)
    failed_count: Mapped[int] = mapped_column(Integer)
    missing_count: Mapped[int] = mapped_column(Integer)
    unresolved_regression: Mapped[bool] = mapped_column(Boolean, default=False)
    scope_violation: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyEfficiencyDelta(Base):
    __tablename__ = "strategy_efficiency_deltas"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        index=True,
    )
    baseline_observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_efficiency_observations.id")
    )
    challenger_observation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_efficiency_observations.id")
    )
    metric_type: Mapped[str] = mapped_column(String(48))
    unit: Mapped[str] = mapped_column(String(24))
    baseline_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    challenger_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    delta_value: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyExperiment(Base):
    __tablename__ = "strategy_experiments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    baseline_strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id")
    )
    challenger_strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id")
    )
    hypothesis: Mapped[str] = mapped_column(Text)
    intended_change: Mapped[str] = mapped_column(Text)
    expected_benefit: Mapped[str] = mapped_column(Text)
    quality_invariants: Mapped[list] = mapped_column(JSON, default=list)
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    applicability: Mapped[dict] = mapped_column(JSON, default=dict)
    required_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="draft")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyPromotionCandidate(Base):
    __tablename__ = "strategy_promotion_candidates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id")
    )
    baseline_strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id")
    )
    applicable_context: Mapped[dict] = mapped_column(JSON, default=dict)
    known_tradeoffs: Mapped[list] = mapped_column(JSON, default=list)
    confidence_basis: Mapped[str] = mapped_column(String(24), default="unknown")
    minimum_valid_comparisons: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), default="candidate")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyExperimentComparison(Base):
    __tablename__ = "strategy_experiment_comparisons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_experiments.id", ondelete="CASCADE"),
        index=True,
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyPromotionComparison(Base):
    __tablename__ = "strategy_promotion_comparisons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_promotion_candidates.id", ondelete="CASCADE"),
        index=True,
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyLearningLink(Base):
    __tablename__ = "strategy_learning_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        nullable=True,
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_experiments.id", ondelete="CASCADE"),
        nullable=True,
    )
    idea_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_ideas.id"), nullable=True
    )
    component_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_solution_components.id"), nullable=True
    )
    assumption_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_problem_assumptions.id"), nullable=True
    )
    disposition: Mapped[str] = mapped_column(String(32), default="unknown")
    relation: Mapped[str] = mapped_column(String(32), default="unknown")
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyLearningObservation(Base):
    __tablename__ = "strategy_learning_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_comparisons.id", ondelete="CASCADE"),
        index=True,
    )
    observation_type: Mapped[str] = mapped_column(String(48))
    trace_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_trace_events.id"), nullable=True
    )
    related_trace_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_trace_events.id"), nullable=True
    )
    stopping_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_stopping_decisions.id"), nullable=True
    )
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategyEvaluationEvent(Base):
    __tablename__ = "strategy_evaluation_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_experiments.id", ondelete="CASCADE"),
        nullable=True,
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_promotion_candidates.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
