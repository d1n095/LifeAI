"""Life Candidate Learning Signals -- see migration 0053's own module docstring for the full
rationale. SIGNAL PRODUCER != TRUTH WRITER: a row here is never a claim about the world, only
a claim that something happened worth a human or reviewed process's later attention."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CANDIDATE_SIGNAL_SOURCE_TYPES = ("message",)
CANDIDATE_SIGNAL_KINDS = ("explicit_memory_candidate", "correction_candidate", "idea_candidate", "unknown")
CANDIDATE_SIGNAL_CONFIDENCES = ("high", "medium", "low", "unknown")
CANDIDATE_SIGNAL_STATUSES = ("unreviewed", "promoted", "dismissed")


class CandidateLearningSignal(Base):
    """One owner-scoped row per candidate signal. Deliberately has NO `authority`/`basis`
    columns -- unlike `founder_memory_notes`/`diagnosis_records`/`capability_records`, this is
    not yet a provenance claim, so giving it that vocabulary would misrepresent what it is.
    `classifier_strategy`/`classifier_confidence` record WHICH heuristic/version produced this
    signal and how sure it was of its OWN classification -- a fact about the classifier, never
    smuggled in as a fact about the world."""

    __tablename__ = "candidate_learning_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="message")
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=True)
    signal_kind: Mapped[str] = mapped_column(String(32))
    classifier_strategy: Mapped[str] = mapped_column(String(64), default="unknown")
    classifier_confidence: Mapped[str] = mapped_column(String(16), default="unknown")
    classifier_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="unreviewed")
    promoted_to_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Migration 0064 (docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §1): which existing
    # entity this signal's source message appears to be ABOUT -- resolved_entity_type is
    # deliberately a loose string, not yet validated against app.active_context.service's
    # closed SUPPORTED_TYPES registry (a reasonable follow-up once real usage exists). Same
    # epistemic status as classifier_confidence: a resolver's own guess, never a truth claim
    # until promote_candidate_signal() (unchanged) turns it into one.
    resolved_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolution_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["promoted_to_note_id", "owner_id"], ["founder_memory_notes.id", "founder_memory_notes.owner_id"], ondelete="SET NULL"
        ),
    )
