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
    module docstring for the full SIGNAL PRODUCER != TRUTH WRITER rationale."""

    __tablename__ = "project_entities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supersedes_entity_id", "owner_id"], ["project_entities.id", "project_entities.owner_id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    derived_from_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_claims.id", ondelete="RESTRICT"), index=True
    )
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
    """An edge between two ProjectEntity rows -- mirrors the pre-existing claim_relationships
    table (migration 0007) exactly, same bare FK precedent for from/to."""

    __tablename__ = "project_entity_relationships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    from_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_entities.id", ondelete="CASCADE"), index=True
    )
    to_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_entities.id", ondelete="CASCADE"), index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class InterpretationProposal(Base):
    """A candidate signal that ONE extracted KnowledgeClaim might be worth turning into
    structured project understanding -- never a claim about the project itself. See migration
    0054's own module docstring for the full architecture. `record_interpretation_proposal()`
    is the only write path; `promote_interpretation_proposal()` is the only path to a real
    ProjectEntity."""

    __tablename__ = "interpretation_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_claims.id", ondelete="CASCADE"), index=True
    )
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
