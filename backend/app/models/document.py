import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, ForeignKeyConstraint, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class DocumentSource(str, enum.Enum):
    upload = "upload"
    website = "website"
    code = "code"
    manual = "manual"
    zip_import = "zip_import"


class IndexStatus(str, enum.Enum):
    """Life Library pipeline steps so material received from a founder can be preserved and
    inspected even when a later step (extraction, embedding) fails or a provider is
    unavailable. `pending`/`indexing` are kept only so already-stored rows and any code that
    still references them keep deserializing; new rows are created as `received` and move
    forward through the granular states below instead.

    Durable-worker package additions: `received` (the Document row exists, but the original
    file hasn't started streaming to durable storage yet — a narrow, request-scoped state),
    `original_storing` (streaming to app/storage/ is in progress), `classifying` (DEL 4:
    modeled alongside `awaiting_classification` for a future auto-classification step — the
    worker today still decides classification synchronously at import time, so nothing
    transitions through either of these two yet, but the UI/API already understand them),
    `cancelled` (DEL 5: a founder deleted the source while the worker was still processing
    it — see app/worker.py's cancellation checks).

    P1 (provider pre-flight verification) additions — splitting the single, undifferentiated
    `failed` into five distinguishable outcomes (see
    docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.7 and app/providers/verification.py):
    `awaiting_provider` (no provider configured for the role at all — a pause, not a
    failure), `blocked_provider` (a provider is configured but the real pre-flight
    verification call did not return ok — invalid key, unreachable, rate-limited, or the
    wrong provider chosen for the role; also a pause, resumed automatically once
    verification succeeds, see app/worker.py's _requeue_blocked_jobs), `storage_failed` (the
    original couldn't be durably stored — terminal, needs a new upload), `extraction_failed`
    (the file's content couldn't be turned into text — terminal, the content itself is the
    problem), `indexing_failed` (extraction succeeded and pre-flight verification passed, but
    the real embedding call still failed — terminal-but-not-automatically-retried, a
    genuinely new, presumably rarer failure). `failed` itself is kept only for the one
    remaining internal-invariant guard (a document with no owner) — every operationally
    reachable failure path now uses one of the five names above instead."""

    pending = "pending"
    received = "received"
    original_storing = "original_storing"
    original_stored = "original_stored"
    extracting = "extracting"
    extracted = "extracted"
    awaiting_classification = "awaiting_classification"
    classifying = "classifying"
    embedding = "embedding"
    indexing = "indexing"
    indexed = "indexed"
    failed = "failed"
    cancelled = "cancelled"
    awaiting_provider = "awaiting_provider"
    blocked_provider = "blocked_provider"
    storage_failed = "storage_failed"
    extraction_failed = "extraction_failed"
    indexing_failed = "indexing_failed"


# 2026-07-28 incident: every status a Document can sit in mid-pipeline, EXCLUDING
# awaiting_provider/blocked_provider (which already have their own dedicated resume path —
# see app/worker.py's _requeue_blocked_jobs) and every terminal status (indexed, and the
# five *_failed/cancelled outcomes). A document left at one of THESE statuses got there
# because the worker PROCESS itself died mid-step (hard kill, OOM, container restart) — no
# exception was ever raised for Python to catch, so nothing set a terminal status. Before
# this constant existed, app/rag/library_import.py's _import_one_file treated a document at
# any of these statuses as an ordinary "duplicate" on a re-run — silently leaving it stuck
# here forever while the job itself could still reach `completed`. See
# app/rag/library_import.py's _resume_incomplete_document and app/worker.py's
# _reconcile_orphaned_documents, the two places this now drives real recovery instead of a
# false "duplicate".
RESUMABLE_INDEX_STATUSES = frozenset(
    {
        IndexStatus.pending,
        IndexStatus.received,
        IndexStatus.original_storing,
        IndexStatus.original_stored,
        IndexStatus.extracting,
        IndexStatus.extracted,
        IndexStatus.awaiting_classification,
        IndexStatus.classifying,
        IndexStatus.embedding,
        IndexStatus.indexing,
    }
)


class ActiveTruthStatus(str, enum.Enum):
    """Founder Knowledge Studio's epistemic status — deliberately separate from IndexStatus
    (which is purely technical: has this been chunked/embedded yet). A document can be fully
    `indexed` and still be `historical` or `disputed`: whether MainAI is allowed to treat its
    content as current fact is a content-classification decision, not a pipeline-progress
    one. See docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md and app/rag/trust.py, which is the actual
    enforcement point — chat.py must never let a `historical`/`superseded`/`disputed` source
    be presented as settled fact just because it scored well on similarity."""

    active = "active"
    historical = "historical"
    proposed = "proposed"
    superseded = "superseded"
    disputed = "disputed"


class KnowledgeClassification(str, enum.Enum):
    vision = "vision"
    architecture = "architecture"
    decisions = "decisions"
    history = "history"
    security = "security"
    general = "general"


class DeletionStatus(str, enum.Enum):
    """Life Library durable-worker package: tracks physical removal of a Document's original
    blob (app/storage/) SEPARATELY from the Document row's own soft-delete
    (Document.deleted_at) — the row can be soft-deleted (invisible to the founder) long
    before its blob is actually, physically purged, and purge failure must be visible on its
    own rather than silently retried forever or silently abandoned. See
    app/rag/library_import.py's maybe_purge_blob()."""

    none = "none"  # nothing scheduled — the normal state for a live (non-deleted) document
    pending = "pending"  # soft-deleted; physical purge not yet attempted or not yet safe (still referenced)
    purged = "purged"  # physically removed from storage
    failed = "failed"  # a purge attempt raised — surfaced separately so it isn't confused with "pending"


class Document(Base):
    __tablename__ = "documents"

    # PR #61 correction pass (post-review, founder-mandated): source_import_batch_id's real
    # constraint is the composite fk_documents_batch_owner below (source_import_batch_id,
    # uploaded_by) -> source_import_batches(id, owner_id) -- the same cross-owner gap already
    # closed on source_import_batch_failures.batch_id. A plain single-column FK on
    # source_import_batch_id alone (documents' RLS only ever checks uploaded_by, never a
    # referenced row's owner) would let a caller point this column at a batch genuinely owned
    # by someone else while still satisfying their own row's RLS WITH CHECK.
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_import_batch_id", "uploaded_by"],
            ["source_import_batches.id", "source_import_batches.owner_id"],
            name="fk_documents_batch_owner",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    source: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource), default=DocumentSource.upload)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Derived UI/corpus-review cache (~1000 chars of extracted text), NOT canonical source
    # truth — originals live at storage_key. Future encryption-only vault sources must not
    # populate this without an explicit preview-derivation policy.
    status: Mapped[IndexStatus] = mapped_column(Enum(IndexStatus), default=IndexStatus.received)
    chunk_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- Founder Knowledge Studio v1 fields (see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md) ---
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # sha256 hex
    media_type: Mapped[str | None] = mapped_column(String(96), nullable=True)  # MIME-ish, e.g. "application/pdf"
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    classification: Mapped[KnowledgeClassification] = mapped_column(
        Enum(KnowledgeClassification), default=KnowledgeClassification.general
    )
    active_truth_status: Mapped[ActiveTruthStatus] = mapped_column(
        Enum(ActiveTruthStatus), default=ActiveTruthStatus.active
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_import_jobs.id"), nullable=True
    )
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Soft delete: see app/routers/library.py's delete_source. A hard DELETE would also work
    # RLS-wise, but soft delete lets a deletion be audited/undone before the next scheduled
    # cleanup, matching the founder's explicit "radering med tydlig bekräftelse" requirement
    # without making it irreversible at the database layer the instant it's clicked.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- STEG 12: audio/video import v1 (see app/rag/media_import.py) ---
    # Both nullable and both None for every non-media Document — only set once
    # index_media_document() has actually produced a transcript. media_duration_seconds
    # drives the player UI's scrubber (STEG 13); transcript_provider records which
    # TranscriptionProvider produced it (today only "mock", see
    # app/providers/transcription.py) purely for UI/debug transparency.
    media_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    transcript_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # STEG 13: the raw uploaded bytes, so GET /api/library/{id}/media (app/routers/library.py)
    # can actually serve them back to an <audio>/<video> element. ONLY ever set for a media
    # import (app/rag/library_import.py's media_kind branch) — every text/document import
    # leaves this NULL, exactly as small as before this column existed.
    # STEG 13 legacy: `media_blob` held a duplicate in-DB copy for the media player.
    # New imports store originals ONLY at `storage_key` (content-addressed durable storage);
    # GET /api/library/{id}/media reads from storage_key first, falling back to media_blob
    # for rows imported before this change. Keeping the column (no migration) preserves
    # backward compatibility without persisting unnecessary plaintext on new imports.
    media_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    # --- Life Library durable-worker package (app/storage/, app/worker.py) ---
    # storage_key: the content-addressed key (see app/storage/local_fs.py) this document's
    # ORIGINAL uploaded bytes live at in the persistent volume — distinct from `checksum`
    # (already sha256, reused as the blob's content hash too) and from `media_blob` (STEG
    # 13's separate in-database copy, kept for now; storage_key is the durable-on-disk copy
    # every document gets, not just media). NULL until the worker has verified a successful,
    # fsync'd write — see IndexStatus.original_stored's docstring: the status only advances
    # past `original_storing` once storage_key is set AND verified.
    storage_key: Mapped[str | None] = mapped_column(String(140), nullable=True, index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_status: Mapped[DeletionStatus] = mapped_column(Enum(DeletionStatus), default=DeletionStatus.none)

    # Life Source Foundation Bootstrap (migration 0037, docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md
    # §E) — which corpus-import batch this document arrived through, if any. NULL for every
    # document uploaded outside a tracked batch (the ordinary Library upload path, unaffected).
    # Only ever set once, at creation, alongside storage_key/file_path — see that column's own
    # comment for why mainai_app cannot UPDATE either of them afterward.
    source_import_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
