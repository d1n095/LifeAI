"""Stage K — structured claims / assumptions (not tied to life_problems)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CLAIM_KINDS = ("contradicts", "assumption", "fact", "superseded", "context_specific")
CLAIM_STATUSES = ("active", "invalidated", "superseded", "disputed")


class StructuredClaim(Base):
    """Durable structured claim/assumption/fact. History is preserved via status + events."""

    __tablename__ = "structured_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["related_entity_id", "owner_id"],
            ["project_entities.id", "project_entities.owner_id"],
            name="fk_structured_claims_related_entity_owner",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["contradicts_entity_id", "owner_id"],
            ["project_entities.id", "project_entities.owner_id"],
            name="fk_structured_claims_contradicts_entity_owner",
            ondelete="SET NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # CONTRADICTS/ASSUMPTION/FACT/SUPERSEDED/CONTEXT-SPECIFIC
    statement: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source: Mapped[str] = mapped_column(String(128), default="unknown")
    status: Mapped[str] = mapped_column(String(24), default="active")
    dependent_refs: Mapped[list] = mapped_column(JSONB, default=list)  # [{kind,id}, ...]
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revalidation_trigger: Mapped[str | None] = mapped_column(String(256), nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    contradicts_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    supersedes_claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StructuredClaimEvent(Base):
    """Append-only history for structured claims — never silent overwrite."""

    __tablename__ = "structured_claim_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
