import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class NoteKind(str, enum.Enum):
    """What kind of project fact a ProjectNote records.

    `fact`/`uncertainty` cover "what are we building, what phase, what's done" and "open
    questions/conflicts" (Fas 2's Current Project State requirement) — `decision`/`blocker`/
    `next_step` remain the original three from the first slice. `idea` (MainAI Core, 2026-07-26)
    is distinct from `decision`: an idea/wish is NOT yet acted on — retrieval and the
    resumption brief must never present one as settled, which is exactly why it isn't just a
    `fact` with a note attached."""

    fact = "fact"
    decision = "decision"
    blocker = "blocker"
    next_step = "next_step"
    uncertainty = "uncertainty"
    idea = "idea"


class NoteStatus(str, enum.Enum):
    """Distinguishes CURRENT project state from HISTORY (see CLAUDE.md's success metric) —
    `open` notes are what a resumption brief surfaces; `resolved`/`superseded` notes remain in
    the table forever (never deleted) as an audit trail of what used to be true and why it
    changed, but are excluded from "what's the state right now"."""

    open = "open"
    resolved = "resolved"
    superseded = "superseded"


class SideIssueClassification(str, enum.Enum):
    """How a non-blocking finding should be triaged (Fas 2 requirement 7) — MainAI STORES
    this classification, it does not compute it autonomously; the caller (agent or founder)
    decides which bucket a finding belongs in. See app/project_memory.py."""

    blocking = "blocking"
    directly_resolvable = "directly_resolvable"
    registered_for_later = "registered_for_later"
    needs_founder_decision = "needs_founder_decision"


class ProjectNote(Base):
    """One project-memory fact: a decision, a blocker, a next step, a plain status fact, or an
    open uncertainty — always with a source citation (`source_type`/`source_ref`, optionally a
    stronger `source_id` link to a ProjectSource row), never a bare, unsourced claim. This is
    the concrete, minimal implementation of "lagra beslut, blockerare och nästa steg med
    källhänvisning" (see CLAUDE.md's "Målet" section and docs/BRANCH_REGISTRY.md, which this
    table is meant to make machine-readable rather than markdown-only convention).

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
    # Optional stronger citation: the actual ProjectSource row this note was derived from
    # (e.g. the exact doc-ingestion or git-snapshot event), when one exists.
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("project_sources.id"), nullable=True)
    # Side-issue triage (Fas 2 requirement 7) — null unless this note is itself a side-finding
    # that needed classifying, not the project's own primary decisions/blockers/next-steps.
    classification: Mapped[SideIssueClassification | None] = mapped_column(Enum(SideIssueClassification), nullable=True)
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
    `git_commit_sha` is what staleness detection compares against the latest ingested git
    snapshot (see app/project_memory.is_checkpoint_stale()).

    Not RLS-protected, same rationale as ProjectNote above."""

    __tablename__ = "project_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    summary: Mapped[str] = mapped_column(Text)
    branch_name: Mapped[str] = mapped_column(String(256))
    open_pr_refs: Mapped[str] = mapped_column(String(512))  # comma-separated, e.g. "#8"
    brief_storage_key: Mapped[str] = mapped_column(String(80))
    brief_sha256: Mapped[str] = mapped_column(String(64))
    git_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
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


class ProjectSource(Base):
    """One ingested source: a governing doc's content (`source_type="doc"`), a local git HEAD
    snapshot (`source_type="git_commit"`), or an agent-supplied GitHub branch/PR snapshot
    (`source_type="github_branch"`/`"github_pr"`) — there is no in-process GitHub client
    anywhere in this codebase, so GitHub state is ingested as structured input the caller
    (the agent, via its own already-authenticated tooling) already fetched, not re-fetched by
    the backend itself. Doc content is stored via the existing content-addressed storage
    backend; `raw_data` holds structured (git/GitHub) snapshots too small/simple to need their
    own blob.

    Not RLS-protected, same rationale as ProjectNote above."""

    __tablename__ = "project_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(32))  # "doc" | "git_commit" | "github_branch" | "github_pr"
    source_ref: Mapped[str] = mapped_column(String(512))  # file path, or "HEAD", or "#8", or branch name
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ingested_by: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ProjectBranchPRStatus(Base):
    """Structured, queryable branch/PR state — distinct from freeform ProjectNote because
    "which branch/PR applies" needs real fields (base/head/mergeable/CI status), not prose.
    A new snapshot for the same (kind, ref) supersedes the previous one (`is_current` flips to
    false, `superseded_at` set) rather than overwriting it in place — same never-delete
    history pattern as ProjectNote.

    Not RLS-protected, same rationale as ProjectNote above."""

    __tablename__ = "project_branch_pr_status"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(16))  # "branch" | "pr"
    ref: Mapped[str] = mapped_column(String(256))  # branch name, or "#8"
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # e.g. "open" | "draft" | "merged" | "closed"
    base_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    head_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    mergeable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ci_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("project_sources.id"), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    recorded_by: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
