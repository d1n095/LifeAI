"""Round 10 M3: ORM models for the Cognitive Regression Gate's durable run records
(migration 0075). See that migration's docstring for the why: gate results must be
EVIDENCE ON RECORD for cross-model comparison, with deterministic objective assertions
and model-judged rubric scores separated at the STORAGE layer. These tables record
verdicts; they enforce nothing — the authorized operational system owns enforcement."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GateExecutionMode(str, enum.Enum):
    deterministic = "deterministic"
    model = "model"


class GateCaseStatus(str, enum.Enum):
    passed = "passed"
    failed = "failed"
    skipped_no_model = "skipped_no_model"


class CognitiveGateRun(Base):
    __tablename__ = "cognitive_gate_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    objective_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CognitiveGateCaseResult(Base):
    __tablename__ = "cognitive_gate_case_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cognitive_gate_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    execution: Mapped[GateExecutionMode] = mapped_column(Enum(GateExecutionMode), nullable=False)
    status: Mapped[GateCaseStatus] = mapped_column(Enum(GateCaseStatus), nullable=False)
    objective_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rubric_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
