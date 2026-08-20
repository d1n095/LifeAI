"""Life Founder/User Memory -- see migration 0049's own module docstring for the full
rationale and reuse map (migration 0042's `authority`/`basis` vocabularies,
`app.active_context.service`'s central object-reference registry).

`FounderMemoryNote` plays the SAME structural role `LifeProblemDecision` already plays for
problem-solving decisions: a mutable row whose `status` transitions (`active` ->
`superseded`/`disputed`), never mutated `content`, self-referential `supersedes_note_id`. No
append-only companion event table -- the row-level supersession chain IS the history, exactly
like `LifeProblemDecision` itself (as opposed to its own satellite `life_problem_events`
log)."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

FOUNDER_MEMORY_NOTE_TYPES = ("decision", "correction", "preference", "goal", "recurring_pattern", "observation", "unknown")
FOUNDER_MEMORY_NOTE_STATUSES = ("active", "superseded", "disputed", "unknown")

# Reused verbatim from migration 0042's LifeProblem/LifeProblemDecision vocabularies -- never a
# second, competing provenance taxonomy.
FOUNDER_MEMORY_AUTHORITIES = (
    "founder", "repeated_founder_preference", "deterministic_source", "inferred_pattern", "ai_interpretation", "unknown"
)
FOUNDER_MEMORY_BASES = ("manual", "deterministic", "imported", "inferred", "ai_interpretation", "unknown")


class FounderMemoryNote(Base):
    """One owner-scoped, evidence-backed fact about the founder/user: an explicit decision,
    correction, preference, expressed goal, observed recurring pattern, or a general
    observation -- `note_type`, always with an explicit `authority`/`basis` pair, NEVER
    inferred by this model or the service layer that writes it. `content` is never rewritten
    in place; a later correction always creates a NEW row via `supersedes_note_id` and flips
    the OLD row's own `status` to `superseded` -- the old row's `content` itself never changes.

    Structurally incapable of representing inferred emotional/psychological state: there is no
    column, no `note_type` value, and no vocabulary anywhere in this model for it -- the same
    "no hidden diagnosis" doctrine `app.context.resolver` already established. `content` may
    only ever be exactly what was explicitly said (`authority='founder'`) or an honestly-labelled
    inference about an observable PATTERN (`authority='inferred_pattern'`), never a claim about
    how the founder feels."""

    __tablename__ = "founder_memory_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    note_type: Mapped[str] = mapped_column(String(24), default="unknown")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="active")
    authority: Mapped[str] = mapped_column(String(40), default="unknown")
    basis: Mapped[str] = mapped_column(String(24), default="unknown")
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    # Free-form pointer to where this note came from -- e.g. "conversation_message:<uuid>",
    # "explicit_statement", "correction_of:<note_id>", an import batch reference. Never itself
    # a second provenance mechanism -- `provenance` (below) carries structured detail.
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    supersedes_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # When the fact/preference/decision itself actually became true, if known -- distinct from
    # observed_at (when THIS system recorded it), matching MemoryThreadMember's own
    # valid_from/source_occurred_at-style distinction between recording time and event time.
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(["supersedes_note_id", "owner_id"], ["founder_memory_notes.id", "founder_memory_notes.owner_id"]),
    )
