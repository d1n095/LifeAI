import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StructuredImportRunStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class StructuredImportItemState(str, enum.Enum):
    discovered = "discovered"
    stored = "stored"
    duplicate = "duplicate"
    parsed = "parsed"
    unsupported = "unsupported"
    failed = "failed"
    deferred = "deferred"


class StructuredImportRun(Base):
    """Checkpoint state owned by one existing ``mainai_jobs`` row.

    It points at the canonical archive Document; it is not a second source or queue.
    """

    __tablename__ = "structured_import_runs"
    __table_args__ = (
        ForeignKeyConstraint(["job_id", "owner_id"], ["mainai_jobs.id", "mainai_jobs.owner_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["source_document_id", "owner_id"], ["documents.id", "documents.uploaded_by"], ondelete="CASCADE"
        ),
        UniqueConstraint("id", "owner_id", name="uq_structured_import_runs_id_owner"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    adapter_key: Mapped[str] = mapped_column(String(128))
    adapter_version: Mapped[str] = mapped_column(String(64))
    source_checksum: Mapped[str] = mapped_column(String(64))
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    discovered_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[StructuredImportRunStatus] = mapped_column(String(16), default=StructuredImportRunStatus.running)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class StructuredImportItem(Base):
    __tablename__ = "structured_import_items"
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "owner_id"], ["structured_import_runs.id", "structured_import_runs.owner_id"], ondelete="CASCADE"),
        UniqueConstraint("run_id", "source_identity", name="uq_structured_import_items_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_identity: Mapped[str] = mapped_column(Text)
    state: Mapped[StructuredImportItemState] = mapped_column(String(16))
    provenance: Mapped[dict] = mapped_column(JSON)
    checkpoint_after: Mapped[dict] = mapped_column(JSON)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
