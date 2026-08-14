import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SourceKind(str, enum.Enum):
    """S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8): document-derived source units.
    `document_chunk` is the normal, fully-traceable path (a live DocumentChunk).
    `document_version` is used when the chunk has since been purged but the KnowledgeVersion
    is still resolvable. `document_record` is the most degraded case: only the Document row
    itself can be vouched for. Each kind gets its own subtype table (see document_source_units),
    never a generic 'document' — see §4.8's "Dokumentgranularitet".

    `message` is the message-source slice of S1C (PR #60 provisional proposal, migration 0037) — see
    message_source_units below. Future source kinds (media_segment, ...) follow the identical
    pattern: a new enum value here plus a new subtype table, never a nullable column bolted
    onto an existing subtype."""

    document_chunk = "document_chunk"
    document_version = "document_version"
    document_record = "document_record"
    message = "message"


class SourceRole(str, enum.Enum):
    """Who actually authored the source content — never inferred from who uploaded it (see
    §4.8's "source_role"). Every DOCUMENT source gets `unknown` permanently (uploading a file
    doesn't tell you who wrote it) — a future, separate source_attributions table handles
    reviewed reattribution, never a silent UPDATE of this column (it's immutable, enforced by
    trg_msu_guard_update). MESSAGE sources (S1C, migration 0037) are the one exception:
    `messages.role` is real, known authorship recorded at write time, so
    trg_msgsu_validate_fields requires source_role to match it exactly (user->founder,
    assistant->assistant, system->system) rather than defaulting to unknown."""

    founder = "founder"
    assistant = "assistant"
    external = "external"
    system = "system"
    unknown = "unknown"


class OccurredAtBasis(str, enum.Enum):
    """How much to trust `occurred_at` (when the original content actually came into being),
    as distinct from `observed_at` (when this system ingested/created the source unit) — see
    §4.8's "Tidsfält korrigerade". `unknown` if and only if `occurred_at IS NULL`."""

    explicit = "explicit"
    source_metadata = "source_metadata"
    inferred = "inferred"
    unknown = "unknown"


class SnapshotStatus(str, enum.Enum):
    """Whether `content_text` is a truthful, exact copy of the original (`exact`), known to
    be unavailable at the granularity this row represents (`degraded`/`missing`), or nulled
    out by a permanent purge (in which case `lifecycle_status='purged'` is the authoritative
    signal, not this field — see the ck_msu_content_matches_snapshot CHECK). `content_text`
    must NEVER be fabricated from other data to fill a `degraded`/`missing` gap."""

    exact = "exact"
    degraded = "degraded"
    missing = "missing"


class LifecycleStatus(str, enum.Enum):
    """`active` (searchable/usable) -> `revoked` (excluded from retrieval/promotion, snapshot
    kept, reversible) -> back to `active` (restore) -> `purged` (content erased, terminal).
    Never written directly via the ORM/a plain UPDATE — see trg_msu_guard_update and
    transition_own_memory_source()/transition_memory_source_admin() in migration 0019, the
    only sanctioned write path for this column."""

    active = "active"
    revoked = "revoked"
    purged = "purged"


class MemorySourceUnit(Base):
    """The atomic, immutable provenance unit every KnowledgeClaim will eventually point to
    via `memory_source_id` (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8) — one
    DocumentChunk/KnowledgeVersion/Document in S1A, one Message/MediaSegment/... in future
    slices, each via its own subtype table (document_source_units here; message_source_units
    is S1C). Never itself a `ConversationSegment` (§4.9), which groups already-existing
    units rather than being a source.

    Row-level immutability (identity fields) and the lifecycle state machine are enforced by
    database triggers and two SECURITY DEFINER functions
    (transition_own_memory_source/transition_memory_source_admin, migration 0019) — this
    model exists for reads and for the INSERT half of find-or-create (see
    app/rag/memory_source.py), never for a plain ORM `UPDATE` of lifecycle fields, which the
    database itself rejects outside those two functions.
    """

    __tablename__ = "memory_source_units"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)

    source_kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind))
    source_identity_key: Mapped[str] = mapped_column(Text)
    source_role: Mapped[SourceRole] = mapped_column(Enum(SourceRole))

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at_basis: Mapped[OccurredAtBasis] = mapped_column(Enum(OccurredAtBasis), default=OccurredAtBasis.unknown)

    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which rule produced content_hash (currently always 'sha256-utf8-v1', see
    # app/rag/memory_source.py's compute_content_hash) -- lets a future hashing-algorithm
    # change tell old rows apart from new ones instead of silently comparing hashes computed
    # two different ways. NULL exactly when content_hash is NULL (mirrors its nullability).
    content_hash_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_status: Mapped[SnapshotStatus] = mapped_column(Enum(SnapshotStatus))

    lifecycle_status: Mapped[LifecycleStatus] = mapped_column(Enum(LifecycleStatus), default=LifecycleStatus.active)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentSourceUnit(Base):
    """1:1 document-provenance subtype of `MemorySourceUnit` (S1A's only subtype — the
    exclusive-arc pattern §4.8 describes, extended to future source kinds via their own
    subtype table, never a nullable `source_X_id` column bolted onto KnowledgeClaim).
    `source_kind` is duplicated from the parent deliberately: it's structurally pinned there
    via the composite FK to `memory_source_units(id, owner_id, source_kind)` (migration
    0019), which is what keeps a purged `document_chunk` row (chunk_id -> NULL) from ever
    colliding with a `document_version`/`document_record` row in the type-scoped partial
    unique indexes.
    """

    __tablename__ = "document_source_units"

    memory_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_source_units.id"), primary_key=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind))

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), index=True)
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_versions.id"), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True
    )


class MemorySourceLifecycleEvent(Base):
    """Append-only audit trail of every lifecycle transition (docs/
    MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8's "Livscykel") — the ONLY writer is
    transition_own_memory_source()/transition_memory_source_admin() (migration 0019);
    `mainai_app` has no direct INSERT/UPDATE/DELETE on this table. `actor_type`/`actor_id`
    are explicit parameters to those functions, never inferred from
    `app.current_user_id` alone, so a system-driven transition (e.g. purge_source's cascade)
    can never be misattributed to the founder personally."""

    __tablename__ = "memory_source_lifecycle_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    memory_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    from_status: Mapped[LifecycleStatus] = mapped_column(Enum(LifecycleStatus))
    to_status: Mapped[LifecycleStatus] = mapped_column(Enum(LifecycleStatus))
    reason: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageSourceUnit(Base):
    """S1C message-source slice (PR #60 provisional proposal, migration 0037,
    docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8/§8) — `Message` as a second
    `MemorySourceUnit` subtype, the exact exclusive-arc pattern `DocumentSourceUnit` already
    established (composite FK to `memory_source_units(id, owner_id, source_kind)`, every field
    permanently immutable via `trg_msgsu_guard_update`, delete forbidden outside account
    erasure via `trg_msgsu_no_delete`). Unlike a document source, `message_id` is always
    required — a message is never "purged to a degraded locator" the way a DocumentChunk can
    be — so there is no chunk_id-style optional field here.

    `trg_msgsu_validate_fields` (migration 0037) additionally verifies `source_role` against
    the message's own real `role` column (see SourceRole's docstring) and, for `exact`
    snapshots, that `content_text`/`content_hash` genuinely match `messages.content` — the same
    rigor `DocumentSourceUnit` already applies to `document_chunks.text`.
    """

    __tablename__ = "message_source_units"

    memory_source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_source_units.id"), primary_key=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind))

    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), index=True)
