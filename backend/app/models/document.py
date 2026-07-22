import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, LargeBinary, String, Text
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
    """Life Library upload consolidation package: granular pipeline steps so material
    received from a founder can be preserved and inspected even when a later step
    (extraction, embedding) fails or a provider is unavailable — see
    docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's successor package. `pending`/`indexing` are kept
    only so already-stored rows and any code that still references them keep deserializing;
    new rows are created as `original_stored` and move forward through the granular states
    below instead. `awaiting_classification` is modeled now but not yet transitioned through
    by the synchronous pipeline (app/rag/library_import.py) — classification is decided at
    import time today — so a future worker can start using it without another migration or
    UI change."""

    pending = "pending"
    original_stored = "original_stored"
    extracting = "extracting"
    extracted = "extracted"
    awaiting_classification = "awaiting_classification"
    embedding = "embedding"
    indexing = "indexing"
    indexed = "indexed"
    failed = "failed"


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


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    source: Mapped[DocumentSource] = mapped_column(Enum(DocumentSource), default=DocumentSource.upload)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[IndexStatus] = mapped_column(Enum(IndexStatus), default=IndexStatus.original_stored)
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
    media_blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
