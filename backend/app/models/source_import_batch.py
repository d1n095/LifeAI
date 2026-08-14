import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SourceImportBatchStatus(str, enum.Enum):
    """Mirrors migration 0037's `ck_sib_status` CHECK exactly. `completed` additionally requires
    the reconciliation CHECK (`ck_sib_completed_reconciles`) to hold -- the database itself
    refuses to let a batch claim completion its own counters don't support, see
    PR #60 provisional proposal §P."""

    discovering = "discovering"
    importing = "importing"
    completed = "completed"
    partial = "partial"
    failed = "failed"


class SourceImportFailureStage(str, enum.Enum):
    storage = "storage"
    parse = "parse"


class SourceImportBatch(Base):
    """A corpus intake session (PR #60 provisional proposal §E) -- e.g. "Founder
    Corpus Import 2026-XX-XX". Tracks discovery counts (populated by the discover job before any
    processing) against outcome counts (populated as processing completes), so completeness can
    be PROVEN rather than self-reported. Ordinary operational data, not evidence -- no
    immutability triggers, unlike memory_source_units."""

    __tablename__ = "source_import_batches"
    __table_args__ = (UniqueConstraint("id", "owner_id", name="uq_sib_id_owner"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)

    label: Mapped[str] = mapped_column(Text)
    source_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SourceImportBatchStatus] = mapped_column(
        Enum(SourceImportBatchStatus), default=SourceImportBatchStatus.discovering
    )

    discovered_files: Mapped[int] = mapped_column(Integer, default=0)
    discovered_archives: Mapped[int] = mapped_column(Integer, default=0)
    discovered_conversations: Mapped[int] = mapped_column(Integer, default=0)
    discovered_messages: Mapped[int] = mapped_column(Integer, default=0)
    discovered_attachments: Mapped[int] = mapped_column(Integer, default=0)
    discovered_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    stored_originals_done: Mapped[int] = mapped_column(Integer, default=0)
    stored_originals_total: Mapped[int] = mapped_column(Integer, default=0)
    parsed_done: Mapped[int] = mapped_column(Integer, default=0)
    parsed_total: Mapped[int] = mapped_column(Integer, default=0)

    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    storage_failed_count: Mapped[int] = mapped_column(Integer, default=0)
    parse_failed_count: Mapped[int] = mapped_column(Integer, default=0)
    unsupported_count: Mapped[int] = mapped_column(Integer, default=0)
    semantic_pending_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def reconciles(self) -> bool:
        """Python-side mirror of migration 0037's `ck_sib_completed_reconciles` CHECK -- lets
        application code decide "is this batch actually done" BEFORE attempting to set
        status=completed (and getting a database error), not just after."""
        return (
            self.stored_originals_total == self.discovered_files
            and self.parsed_total == self.stored_originals_done
            and self.failed_count == self.storage_failed_count + self.parse_failed_count
            and self.discovered_files == self.stored_originals_done + self.storage_failed_count
            and self.parsed_done + self.unsupported_count + self.semantic_pending_count + self.parse_failed_count
            == self.stored_originals_done
        )


class SourceImportBatchFailure(Base):
    """One row per source that failed during a batch (PR #60 provisional proposal
    §J) -- `retryable` distinguishes a transient failure (provider down, worker crashed) a future
    retry pass should pick up automatically from a permanent one (corrupt file, genuinely
    unparseable) that needs either a corrected source or an explicit override."""

    __tablename__ = "source_import_batch_failures"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "owner_id"],
            ["source_import_batches.id", "source_import_batches.owner_id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    source_ref: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_stage: Mapped[SourceImportFailureStage] = mapped_column(Enum(SourceImportFailureStage))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
