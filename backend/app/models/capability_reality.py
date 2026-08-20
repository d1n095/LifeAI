"""Life Capability Reality / Self-Model -- see migration 0048's own module docstring for the
full rationale and reuse map (`intelligence_evidence`, `coordination_agents`, migration 0042's
`authority` vocabulary).

`CapabilityRecord` is the live, mutable "current state" row for ONE owner-scoped capability --
distinct from `CapabilityObservationEvent` (append-only history) the same way
`AgentScopeLease`/`AgentDispatchExecution` are distinct from their own event logs."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CAPABILITY_STATUSES = ("verified_available", "configured_unavailable", "configured_disabled", "planned", "unknown")

# Reused verbatim from migration 0042's LifeProblem/LifeProblemDecision `authority` vocabulary
# -- never a second, competing provenance taxonomy.
CAPABILITY_AUTHORITIES = (
    "founder", "repeated_founder_preference", "deterministic_source", "inferred_pattern", "ai_interpretation", "unknown"
)

CAPABILITY_OBSERVATION_EVENT_TYPES = (
    "status_changed", "verification_recorded", "success_recorded", "failure_recorded",
    "gap_recorded", "observation_reasserted",
)


class CapabilityRecord(Base):
    """One owner-scoped, queryable fact: "as of now, can Life actually do X." Never inferred
    from an executable/binary/adapter merely existing -- `status` is always an explicit,
    caller-supplied classification (see `app.capability_reality.service`'s own module
    docstring). `capability_key` is a stable identifier (e.g. `"agent_dispatch.codex"`,
    `"document_ingestion.pdf_extraction"`); `domain` is an open, non-enumerated grouping label,
    matching `intelligence_executions.domain`'s own precedent of staying extensible rather than
    a fixed CHECK-constrained vocabulary."""

    __tablename__ = "capability_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    capability_key: Mapped[str] = mapped_column(String(128))
    domain: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    authority: Mapped[str] = mapped_column(String(40), default="unknown")
    required_permissions: Mapped[list] = mapped_column(JSON, default=list)
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    known_limitations: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("coordination_agents.id"), nullable=True)
    last_verification_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["last_verification_evidence_id", "owner_id"], ["intelligence_evidence.id", "intelligence_evidence.owner_id"], ondelete="SET NULL"
        ),
    )


class CapabilityObservationEvent(Base):
    """Append-only history of what was observed about ONE capability over time -- application
    code only ever INSERTs here (migration 0048's DB trigger enforces this), same pattern as
    `AgentWorkAssignmentEvent`/`IntelligenceEvidence`."""

    __tablename__ = "capability_observation_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    capability_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["capability_record_id", "owner_id"], ["capability_records.id", "capability_records.owner_id"], ondelete="CASCADE"
        ),
    )
