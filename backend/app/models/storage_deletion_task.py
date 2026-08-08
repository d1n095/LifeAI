import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StorageDeletionReason(str, enum.Enum):
    """Closed set matching migration 0021's `ck_storage_deletion_tasks_reason`, widened by
    migration 0024 (Pass 31) to add `rejected_upload_cleanup` -- a future further reason adds
    its own value here AND to that CHECK constraint in the same change, never a
    caller-supplied free-text string."""

    account_erasure = "account_erasure"
    # Pass 31: a rejected (e.g. empty) upload whose confirmed-unreferenced physical blob failed
    # to delete immediately -- see app/rag/blob_references.py's
    # enqueue_rejected_upload_cleanup_task() for how this reason's tasks are created, and
    # migration 0024's module docstring for the incident this closes.
    rejected_upload_cleanup = "rejected_upload_cleanup"


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
    # Pass 27: SQLAlchemy 2.0 defaults to fetching server-generated columns (created_at/
    # updated_at below) back via an `INSERT ... RETURNING` clause immediately after insert —
    # but PostgreSQL requires SELECT privilege on any column named in a RETURNING clause, on
    # top of INSERT itself. mainai_app now has INSERT ONLY on this table (see
    # backend/scripts/s1a_privilege_policy.py) — the ordinary account-erasure INSERT path
    # would otherwise fail with "permission denied" the moment it tried to insert a row at
    # all, purely because of this eager-fetch, not because of anything the caller actually
    # asked to read. Disabled here since nothing in this codebase reads a freshly-inserted
    # task's created_at/updated_at before the next real DB round-trip anyway.
    __mapper_args__ = {"eager_defaults": False}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    storage_key: Mapped[str] = mapped_column(String(140))
    # Pass 27: native_enum=False + create_constraint=False -- migration 0021 defines these as
    # plain `varchar(N) + CHECK`, not a native Postgres ENUM TYPE (`CREATE TYPE ... AS ENUM`).
    # The default `Enum(...)` would silently describe a DIFFERENT database type than what the
    # migration actually created -- harmless today only because SQLAlchemy's bind processor
    # sends plain strings for either representation, but a real lie about the schema (wrong
    # future autogenerate diffs, unverified assumption that could break on a dialect/version
    # change). `create_constraint=False` because migration 0021's own CHECK constraints
    # (ck_storage_deletion_tasks_reason/_status) are the actual, already-applied source of
    # truth -- this type must never try to also create its own. Lengths match the migration's
    # varchar(32)/varchar(16) exactly.
    reason: Mapped[StorageDeletionReason] = mapped_column(
        Enum(StorageDeletionReason, native_enum=False, create_constraint=False, length=32),
        default=StorageDeletionReason.account_erasure,
    )
    status: Mapped[StorageDeletionStatus] = mapped_column(
        Enum(StorageDeletionStatus, native_enum=False, create_constraint=False, length=16),
        default=StorageDeletionStatus.pending,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Pass 28 (migration 0022): worker backoff for a `failed` task -- set only on a failed
    # attempt (see app/account/erasure.py's attempt_storage_deletion_task), never on
    # pending/processing. NULL for a task that has never failed.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
