import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProjectEntity(Base):
    """Structured project/founder understanding derived from a KnowledgeClaim -- the P4 layer
    docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md's §4.2/§6.4 describes. Never created directly;
    the only path here is app.project_entities.service.promote_interpretation_proposal(),
    which always requires the caller's own explicit authority/basis. See migration 0054's own
    module docstring for the full SIGNAL PRODUCER != TRUTH WRITER rationale.

    `derived_from_claim_id` is owner-anchored via a composite FK (see migration 0056) -- a
    bare FK only proves the referenced claim exists, not that it belongs to the same owner,
    exactly the defect class app.models.knowledge_claim.KnowledgeClaim's own module docstring
    already documents and fixes for memory_source_id."""

    __tablename__ = "project_entities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_entity_id", "owner_id"], ["project_entities.id", "project_entities.owner_id"],
        ),
        ForeignKeyConstraint(
            ["derived_from_claim_id", "owner_id"], ["knowledge_claims.id", "knowledge_claims.owner_id"],
            ondelete="RESTRICT", name="fk_project_entities_derived_from_claim_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    derived_from_claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    supersedes_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    authority: Mapped[str] = mapped_column(String(40), default="unknown")
    basis: Mapped[str] = mapped_column(String(40), default="unknown")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProjectEntityRelationship(Base):
    """An edge between two ProjectEntity rows. Originally mirrored the pre-existing
    claim_relationships table (migration 0007)'s bare-FK precedent for from/to -- migration
    0056 corrected that to owner-anchored composite FKs instead: "existing precedent" is not
    automatically "correct precedent", and claim_relationships (2026-07-20) predates this
    mission's own owner-anchoring discipline that migration 0019 already established
    elsewhere in this same codebase."""

    __tablename__ = "project_entity_relationships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["from_entity_id", "owner_id"], ["project_entities.id", "project_entities.owner_id"],
            ondelete="CASCADE", name="fk_project_entity_relationships_from_entity_owner",
        ),
        ForeignKeyConstraint(
            ["to_entity_id", "owner_id"], ["project_entities.id", "project_entities.owner_id"],
            ondelete="CASCADE", name="fk_project_entity_relationships_to_entity_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    to_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    relationship_type: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InterpretationProposal(Base):
    """A candidate signal that ONE extracted KnowledgeClaim might be worth turning into
    structured project understanding -- never a claim about the project itself. See migration
    0054's own module docstring for the full architecture. `record_interpretation_proposal()`
    is the only write path; `promote_interpretation_proposal()` is the only path to a real
    ProjectEntity.

    `source_claim_id` is owner-anchored via a composite FK (see migration 0056)."""

    __tablename__ = "interpretation_proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_claim_id", "owner_id"], ["knowledge_claims.id", "knowledge_claims.owner_id"],
            ondelete="CASCADE", name="fk_interpretation_proposals_source_claim_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    proposed_entity_type: Mapped[str] = mapped_column(String(32))
    classifier_strategy: Mapped[str] = mapped_column(String(64), default="unknown")
    classifier_confidence: Mapped[str] = mapped_column(String(16), default="unknown")
    classifier_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="unreviewed")
    promoted_to_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
