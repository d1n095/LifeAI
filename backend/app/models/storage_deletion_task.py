import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StorageDeletionReason(str, enum.Enum):
    """Closed set matching migration 0021's `ck_storage_deletion_tasks_reason` — a future
    second reason (e.g. a scheduled retention purge) adds its own value here AND to that
    CHECK constraint in the same change, never a caller-supplied free-text string."""

    account_erasure = "account_erasure"


class StorageDeletionStatus(str, enum.Enum):
    """See migration 0021's module docstring for the full lifecycle. `retained_shared` is a
    terminal SUCCESS, not a failure: the erased owner's own rows are already gone, and the
    physical blob correctly survives because a different owner's live Document/ImportJob
    still needs it (storage_key_still_referenced_global(), migration 0020)."""

    pending = "pending"
    processing = "processing"
    purged = "purged"
    retained_shared = "retained_shared"
    failed = "failed"


class StorageDeletionTask(Base):
    """Durable, retryable record of a physical blob an account erasure still needs to
    (attempt to) delete — see migration 0021's module docstring for why this table exists at
    all (an in-memory-only best-effort delete would silently orphan a file forever if the
    process crashed in the narrow window between the DB erasure commit and the delete
    attempt). Deliberately has NO `owner_id`/FK to `users.id` — see that migration's
    docstring for why it must outlive the very account whose erasure created it, and why no
    PII is stored here at all (a `storage_key` is a content hash, not personal data on its
    own; `operation_id` is a synthetic, unattributable identifier for one erasure run, never
    the erased user's own id).
    """

    __tablename__ = "storage_deletion_tasks"
    __table_args__ = (
        UniqueConstraint("operation_id", "storage_key", name="uq_storage_deletion_tasks_operation_key"),
        CheckConstraint("attempt_count >= 0", name="ck_storage_deletion_tasks_attempt_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    storage_key: Mapped[str] = mapped_column(String(140))
    reason: Mapped[StorageDeletionReason] = mapped_column(
        Enum(StorageDeletionReason), default=StorageDeletionReason.account_erasure
    )
    status: Mapped[StorageDeletionStatus] = mapped_column(
        Enum(StorageDeletionStatus), default=StorageDeletionStatus.pending, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
