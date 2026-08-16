"""Deterministic strategy synthesis recipes over canonical Life evidence."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StrategySynthesisCase(Base):
    __tablename__ = "strategy_synthesis_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_key: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    predecessor_case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_synthesis_cases.id"), nullable=True
    )
    predecessor_strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id"), nullable=True
    )
    candidate_strategy_key: Mapped[str] = mapped_column(String(128))
    candidate_strategy_version: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_tasks.id"), nullable=True
    )
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_problems.id"), nullable=True
    )
    domain: Mapped[str | None] = mapped_column(String(128), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(24), default="unknown")
    purpose: Mapped[str] = mapped_column(Text)
    improvement_dimensions: Mapped[list] = mapped_column(JSON, default=list)
    quality_invariants: Mapped[list] = mapped_column(JSON, default=list)
    applicability: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_benefits: Mapped[list] = mapped_column(JSON, default=list)
    expected_tradeoffs: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    next_component_sequence: Mapped[int] = mapped_column(Integer, default=1)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisInput(Base):
    __tablename__ = "strategy_synthesis_inputs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_cases.id", ondelete="CASCADE"),
        index=True,
    )
    source_kind: Mapped[str] = mapped_column(String(48))
    work_strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id"), nullable=True
    )
    strategy_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategy_executions.id"), nullable=True
    )
    intelligence_idea_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_ideas.id"), nullable=True
    )
    intelligence_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    intelligence_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_executions.id"), nullable=True
    )
    comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_comparisons.id"), nullable=True
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_experiments.id"), nullable=True
    )
    engineering_lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("engineering_lessons.id"), nullable=True
    )
    solution_component_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_solution_components.id"), nullable=True
    )
    assumption_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_problem_assumptions.id"), nullable=True
    )
    specialist_contribution_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_specialist_contributions.id"),
        nullable=True,
    )
    learning_observation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_learning_observations.id"),
        nullable=True,
    )
    stopping_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_stopping_decisions.id"), nullable=True
    )
    disposition: Mapped[str] = mapped_column(String(24), default="unknown")
    reason: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    supporting_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisComponent(Base):
    __tablename__ = "strategy_synthesis_components"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_cases.id", ondelete="CASCADE"),
        index=True,
    )
    input_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_synthesis_inputs.id"), index=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer)
    component_kind: Mapped[str] = mapped_column(String(48), default="unknown")
    description: Mapped[str] = mapped_column(Text)
    modification_intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    disposition: Mapped[str] = mapped_column(String(24), default="unknown")
    applicability: Mapped[dict] = mapped_column(JSON, default=dict)
    method_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    assumption_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_problem_assumptions.id"), nullable=True
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text)
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisConflict(Base):
    __tablename__ = "strategy_synthesis_conflicts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_cases.id", ondelete="CASCADE"),
        index=True,
    )
    left_component_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_components.id"),
        nullable=True,
    )
    right_component_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_components.id"),
        nullable=True,
    )
    assumption_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("life_problem_assumptions.id"), nullable=True
    )
    severity: Mapped[str] = mapped_column(String(16), default="hard")
    status: Mapped[str] = mapped_column(String(24), default="unresolved")
    description: Mapped[str] = mapped_column(Text)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisMaterialization(Base):
    __tablename__ = "strategy_synthesis_materializations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_cases.id", ondelete="CASCADE"),
        index=True,
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("work_strategies.id"), index=True
    )
    recipe_fingerprint: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisEvaluationLink(Base):
    __tablename__ = "strategy_synthesis_evaluation_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    materialization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_materializations.id", ondelete="CASCADE"),
        index=True,
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_experiments.id"), nullable=True
    )
    comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_comparisons.id"), nullable=True
    )
    promotion_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_promotion_candidates.id"),
        nullable=True,
    )
    relation: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisLessonLink(Base):
    __tablename__ = "strategy_synthesis_lesson_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_cases.id", ondelete="CASCADE"),
        index=True,
    )
    component_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_components.id"),
        nullable=True,
    )
    engineering_lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("engineering_lessons.id"), index=True
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("intelligence_evidence.id"), nullable=True
    )
    relation: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StrategySynthesisEvent(Base):
    __tablename__ = "strategy_synthesis_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_cases.id", ondelete="CASCADE"),
        index=True,
    )
    conflict_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_synthesis_conflicts.id", ondelete="CASCADE"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
