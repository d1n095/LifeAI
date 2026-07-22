import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ImportJobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    partial = "partial"  # some files succeeded, some failed — see app/rag/library_import.py
    # Life Library durable-worker package: the founder deleted every source this job would
    # still have produced (or the job itself, before it produced anything) while it was
    # still pending/running — see app/routers/library.py's delete_source and
    # app/worker.py's cancellation checks between pipeline steps.
    cancelled = "cancelled"


class ImportJob(Base):
    """Tracks one import operation (a single file or a ZIP package) end to end — the
    resumability/idempotency anchor DEL 10 asks for. There is no external worker/queue
    tonight (see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md's "Background jobs" section for why and
    what a real one would need) — this row is written and updated synchronously by the same
    request/background-task that does the work, but the schema itself doesn't assume that:
    a future worker can pick up a `pending`/`running` row exactly like a synchronous caller
    does today.

    `source_checksum` + `owner_id` is what makes re-uploading the same ZIP idempotent (see
    app/rag/library_import.py) — a second import with an identical checksum for the same
    owner returns the original completed job instead of re-processing.
    """

    __tablename__ = "knowledge_import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    status: Mapped[ImportJobStatus] = mapped_column(Enum(ImportJobStatus), default=ImportJobStatus.pending)
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Per-file outcome: [{"filename": ..., "status": "indexed"|"failed"|"skipped", "reason": ..., "source_id": ...}]
    file_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # --- STEG 11: retry/coordination (app/jobs/retry.py, app/jobs/lock.py) ---
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    # None until a failure has actually happened; then True/False per app/jobs/retry.py's
    # is_transient_error() — lets the Library UI show "kommer försökas igen automatiskt" vs.
    # "kräver en ny manuell import" instead of a single generic "misslyckades".
    last_failure_transient: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Life Library durable-worker package (app/storage/, app/worker.py) ---
    # The RAW uploaded package (a single file or a ZIP) as streamed durably to app/storage/
    # by POST /api/library/import BEFORE any response is sent — source_checksum above is
    # already this blob's sha256 (reused, not duplicated). The worker opens this file to do
    # the actual extraction/indexing work, instead of receiving the bytes in-process.
    source_storage_key: Mapped[str | None] = mapped_column(String(140), nullable=True)
    source_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_media_type: Mapped[str | None] = mapped_column(String(96), nullable=True)

    # Worker claim/lease (app/worker.py's claim_next_job — Postgres FOR UPDATE SKIP LOCKED,
    # not a Redis lock: Postgres is the source of truth for which worker owns a job, exactly
    # as this package's design requires). locked_by identifies the claiming
    # worker/container (settings.worker_id); lease_expires_at is what lets a DIFFERENT
    # worker safely reclaim a job whose owner crashed or was killed mid-processing — see
    # claim_next_job's docstring. last_heartbeat_at is renewed alongside the lease on every
    # per-step progress update, surfaced on GET /api/library/ops/status.
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
