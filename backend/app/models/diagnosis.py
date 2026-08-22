"""Life Causal Diagnosis Interface -- see migration 0050's own module docstring for the full
rationale and reuse map (migration 0042's `authority`/`basis` vocabularies, `intelligence_
evidence` as the required grounding for a `proven_cause`, `app.active_context.service`'s
central object-reference registry).

`DiagnosisRecord` plays the SAME structural role `FounderMemoryNote` (migration 0049) already
plays: a mutable row whose `epistemic_stage` transitions, never mutated `observation`,
self-referential `supersedes_diagnosis_id`. No append-only companion event table -- the
row-level supersession chain IS the history, the same choice already made for
`founder_memory_notes`."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Bootstrap examples, not a permanent taxonomy -- see migration 0050's own docstring and the
# mission's own "these categories are bootstrap examples only... must be extensible/revisable."
DIAGNOSIS_CATEGORIES = (
    "code_regression", "concurrency_timing", "stale_state", "environment_configuration",
    "external_service_failure", "dependency_failure", "authorization_blocker", "missing_capability", "unknown",
)
DIAGNOSIS_EPISTEMIC_STAGES = ("observed", "hypothesis", "proven_cause", "ruled_out")

# Reused verbatim from migration 0042's LifeProblem/LifeProblemDecision vocabularies -- never a
# third, competing provenance taxonomy (already reused once for founder_memory_notes).
DIAGNOSIS_AUTHORITIES = (
    "founder", "repeated_founder_preference", "deterministic_source", "inferred_pattern", "ai_interpretation", "unknown"
)
DIAGNOSIS_BASES = ("manual", "deterministic", "imported", "inferred", "ai_interpretation", "unknown")


class DiagnosisRecord(Base):
    """One owner-scoped causal diagnosis: an `observation` (the raw, factual thing that was
    actually seen -- immutable once created, never itself an interpretation), a
    `hypothesis_category` + `hypothesis_reasoning` (a candidate cause, NOT yet proven), and an
    `epistemic_stage` that distinguishes `observed` (nothing hypothesized yet) / `hypothesis`
    (a candidate cause proposed, unproven) / `proven_cause` (grounded in real evidence -- the
    database itself refuses this transition without `proven_evidence_id` set, see migration
    0050's own CHECK constraint) / `ruled_out` (a hypothesis that evidence contradicted).

    A failed step never automatically implies a code regression -- `hypothesis_category`
    defaults to `unknown` and every value here is an explicit, caller-supplied classification,
    the same "caller supplies classifications; this module neither invokes providers nor
    infers them" doctrine `app.capability_reality.service`/`app.founder_memory.service` already
    establish."""

    __tablename__ = "diagnosis_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    observation: Mapped[str] = mapped_column(Text)
    hypothesis_category: Mapped[str] = mapped_column(String(32), default="unknown")
    hypothesis_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    epistemic_stage: Mapped[str] = mapped_column(String(16), default="observed")
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    authority: Mapped[str] = mapped_column(String(40), default="unknown")
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    proven_evidence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    supersedes_diagnosis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(["proven_evidence_id", "owner_id"], ["intelligence_evidence.id", "intelligence_evidence.owner_id"], ondelete="SET NULL"),
        ForeignKeyConstraint(["supersedes_diagnosis_id", "owner_id"], ["diagnosis_records.id", "diagnosis_records.owner_id"]),
    )
