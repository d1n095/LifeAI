import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClaimStatus(str, enum.Enum):
    """Deliberately a separate, smaller enum from Document's ActiveTruthStatus (which also
    has `superseded`) — a claim doesn't get individually superseded, its SOURCE does (via
    SourceRelationship); a claim's own status only tracks whether it's currently treated as
    settled fact (active), no-longer-current (historical), not-yet-decided (proposed), or
    actively contested (disputed). Matches STEG 10's explicit status list exactly."""

    active = "active"
    historical = "historical"
    proposed = "proposed"
    disputed = "disputed"


class ClaimType(str, enum.Enum):
    """P3 (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.1): a coarse classification of WHAT a
    claim actually is, extracted in the same AI call as the claim text itself for every NEW
    claim (see app/rag/claims.py) — the input P4's later interpretation queue reads to decide
    whether a claim should become a project_entities row (idea/decision/task_reference) or
    stay a plain fact. A wrong/uncertain classification is `uncategorized`, never a guess
    dressed up as one of the other six values.

    A claim created before P3 existed (or by any extraction pass that predates this column)
    starts at `uncategorized` too, but that is NOT its permanent state: app/rag/claims.py's
    backfill_claim_types() retroactively classifies exactly these rows in place, in bounded
    batches, without ever creating a new KnowledgeClaim — see that function's docstring for
    the idempotency/retry contract. `uncategorized` therefore means one of two things
    depending on `extraction_version`: "not yet classified, still a backfill candidate" (an
    old extraction_version) or "classified and genuinely ambiguous, settled" (the current
    extraction_version) — never confuse the two without checking extraction_version too."""

    idea = "idea"
    decision = "decision"
    task_reference = "task_reference"
    vision = "vision"
    technical = "technical"
    historical = "historical"
    uncategorized = "uncategorized"


class ClaimConfidence(str, enum.Enum):
    """The UI-facing confidence bucket DEL 8 asks for: certain, likely, uncertain, conflict,
    no basis. Computed by app/rag/trust.py's assess_claim_confidence() from the claim's
    grounding_score plus its claim_relationships — never from the extracting model's own
    self-reported confidence (same "don't trust the model's self-assessment" principle
    app/rag/trust.py already applies at the source level)."""

    certain = "certain"
    likely = "likely"
    uncertain = "uncertain"
    conflict = "conflict"
    no_basis = "no_basis"


class KnowledgeClaim(Base):
    """A single, testable factual statement extracted from one chunk of one source — the
    claim-level layer STEG 10 asks for, sitting below Document/SourceRelationship (which
    operate on whole sources) so a founder or MainAI can reason about "is THIS specific
    statement well-supported" rather than only "is this whole document trustworthy".

    Bound to source_id (always), version_id and chunk_id (both nullable: chunk_id is set to
    NULL, not cascaded away, when its DocumentChunk is purged on source deletion — see
    app/routers/library.py's delete_source — so the claim itself survives as revision
    history even though its live grounding is gone; any retrieval path MUST additionally
    check the source's own deleted_at, exactly like app/rag/vector_store.py already does for
    chunks, since a claim with a NULL chunk_id is not automatically "safe").

    grounding_score is a plain word-overlap ratio between claim_text and the source chunk it
    was extracted from (see app/rag/claims.py — the same kind of cheap, auditable heuristic
    app/context/resolver.py's _topic_overlap already uses, not a claim of semantic
    understanding) — the objective signal `confidence` above is computed from, specifically
    so a claim can never be marked "certain" purely because the extracting model said so.
    """

    __tablename__ = "knowledge_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_versions.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True, index=True)
    claim_text: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[ClaimType] = mapped_column(Enum(ClaimType), default=ClaimType.uncategorized)
    status: Mapped[ClaimStatus] = mapped_column(Enum(ClaimStatus), default=ClaimStatus.proposed)
    confidence: Mapped[ClaimConfidence] = mapped_column(Enum(ClaimConfidence), default=ClaimConfidence.uncertain)
    grounding_score: Mapped[float] = mapped_column(Float, default=0.0)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    extraction_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, migration 0019): the claim's exact
    # primary provenance unit, nullable during the cutover — phase 1 of §4.8's six-phase plan.
    # source_id/version_id/chunk_id above stay authoritative until phase 4; this column is
    # additive only. ON DELETE RESTRICT (not SET NULL): a bare DELETE on memory_source_units
    # must never silently orphan a claim — the controlled purge/erasure functions delete the
    # claim itself first instead.
    memory_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_source_units.id", ondelete="RESTRICT"), nullable=True, index=True
    )
