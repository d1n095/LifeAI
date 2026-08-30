"""Stage J — founder personal intent bindings (migration 0065)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class FounderIntentBinding(Base):
    """Learned mapping: founder raw phrasing → interpreted intent (+ optional canonical entity).

    Interpretation only — never authority. Canonical entity titles are never rewritten from here.
    """

    __tablename__ = "founder_intent_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["canonical_entity_id", "owner_id"],
            ["project_entities.id", "project_entities.owner_id"],
            ondelete="SET NULL",
            name="fk_founder_intent_bindings_entity_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    raw_expression: Mapped[str] = mapped_column(Text)
    phrase_normalized: Mapped[str] = mapped_column(String(512), index=True)
    interpreted_intent: Mapped[str] = mapped_column(Text)
    canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    retrieval_trigger: Mapped[str | None] = mapped_column(String(256), nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="active")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FounderIntentCorrection(Base):
    """Append-only correction history for a binding. Prior intent is preserved."""

    __tablename__ = "founder_intent_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["binding_id", "owner_id"],
            ["founder_intent_bindings.id", "founder_intent_bindings.owner_id"],
            ondelete="CASCADE",
            name="fk_founder_intent_corrections_binding_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    prior_intent: Mapped[str] = mapped_column(Text)
    corrected_intent: Mapped[str] = mapped_column(Text)
    wrong_terminology: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    prior_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    corrected_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
