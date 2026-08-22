import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkCandidate(Base):
    """A claim that a piece of structured project understanding (ProjectEntity) MIGHT be
    worth turning into real, governed MainAI work -- never a claim that it is authorized.
    See migration 0055's own module docstring for the full architecture. The only path to a
    real MainAIGoal is app.work_candidates.service.authorize_work_candidate().

    `source_entity_id` is owner-anchored via a composite FK (see migration 0056) --
    `authorized_goal_id` deliberately stays a bare FK; see migration 0056's own module
    docstring for why that one reference carries no actual cross-owner risk."""

    __tablename__ = "work_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_entity_id", "owner_id"], ["project_entities.id", "project_entities.owner_id"],
            ondelete="CASCADE", name="fk_work_candidates_source_entity_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    title: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependencies: Mapped[list] = mapped_column(JSONB, default=list)
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    status: Mapped[str] = mapped_column(String(24), default="unreviewed")
    authorized_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="SET NULL"), nullable=True
    )
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classifier_strategy: Mapped[str] = mapped_column(String(64), default="unknown")
    classifier_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
