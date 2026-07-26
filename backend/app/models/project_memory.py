import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class NoteKind(str, enum.Enum):
    """What kind of project fact a ProjectNote records — the three categories the founder
    (and MainAI itself, later) need to distinguish when resuming work: a choice already made,
    something currently stopping progress, and what to do once unblocked."""

    decision = "decision"
    blocker = "blocker"
    next_step = "next_step"


class NoteStatus(str, enum.Enum):
    """Distinguishes CURRENT project state from HISTORY (see CLAUDE.md's success metric) —
    `open` notes are what a resumption brief surfaces; `resolved`/`superseded` notes remain in
    the table forever (never deleted) as an audit trail of what used to be true and why it
    changed, but are excluded from "what's the state right now"."""

    open = "open"
    resolved = "resolved"
    superseded = "superseded"


class ProjectNote(Base):
    """One project-memory fact: a decision, a blocker, or a next step — always with a source
    citation (`source_type`/`source_ref`), never a bare, unsourced claim. This is the concrete,
    minimal implementation of "lagra beslut, blockerare och nästa steg med källhänvisning"
    (see CLAUDE.md's "Målet" section and docs/BRANCH_REGISTRY.md, which this table is meant to
    make machine-readable rather than markdown-only convention).

    Not RLS-protected — like `provider_config`/`provider_verification_checks` (migration
    0001/0013): this is founder-wide project state, not per-user data, in a founder-only
    system."""

    __tablename__ = "project_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[NoteKind] = mapped_column(Enum(NoteKind))
    status: Mapped[NoteStatus] = mapped_column(Enum(NoteStatus), default=NoteStatus.open)
    content: Mapped[str] = mapped_column(Text)
    # e.g. source_type="pr", source_ref="#8"; source_type="commit", source_ref="169596617e14";
    # source_type="doc", source_ref="CLAUDE.md#Merge-regeln". Never optional — an unsourced
    # note is exactly the "guessing instead of citing" failure mode this table exists to
    # prevent (see CLAUDE.md's grundprincip and the P7A plan's evidence-requirement precedent).
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(256))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProjectCheckpoint(Base):
    """One point-in-time snapshot of project state, durably stored so a NEW Claude session
    (or the founder) can resume without re-deriving it from scratch. `brief_storage_key`
    points at the full resumption-brief markdown via the exact same content-addressed storage
    used for documents (app/storage — see get_storage()) — this table just indexes it.

    Not RLS-protected, same rationale as ProjectNote above."""

    __tablename__ = "project_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    summary: Mapped[str] = mapped_column(Text)
    branch_name: Mapped[str] = mapped_column(String(256))
    open_pr_refs: Mapped[str] = mapped_column(String(512))  # comma-separated, e.g. "#8"
    brief_storage_key: Mapped[str] = mapped_column(String(80))
    brief_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ProjectCheckpointNote(Base):
    """Which notes were open at the moment a given checkpoint was created — an explicit,
    queryable link so a checkpoint's "current state" claim can be verified against the actual
    ProjectNote rows it was built from, not just trusted from the brief's free text."""

    __tablename__ = "project_checkpoint_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    checkpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("project_checkpoints.id"), index=True)
    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("project_notes.id"), index=True)
