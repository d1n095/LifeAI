import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BackfillRunMode(str, enum.Enum):
    """`dry_run` resolves and tallies every candidate exactly as a real run would, but never
    writes `memory_source_id` (see app/rag/memory_source_backfill.py's dry_run docstring).
    `real` actually links claims. A run row's mode is fixed at creation — never changed in
    place, so the durable record can never blur a rehearsal with a real run."""

    dry_run = "dry_run"
    real = "real"


class BackfillRunStatus(str, enum.Enum):
    """`pending` until the first advance_backfill_run() call. `running` while candidates may
    remain. `completed` ONLY when a call reports zero remaining candidates for this owner —
    never inferred from a batch cap being hit (see migration 0025's module docstring: "no
    fabricated completion claims"). `failed` is reserved for an unexpected exception escaping
    the backfill call entirely; ordinary per-claim failures stay in failed_count without
    aborting the run. `cancelled` is operator-triggered."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class MemorySourceBackfillRun(Base):
    """Durable production backfill-run record (migration 0025) — the last blocker
    app/rag/memory_source_backfill.py's own module docstring names before that module's
    backfill_memory_source_units() may ever be run against production. See that migration's
    module docstring for the full design rationale (cursor semantics, snapshot counters,
    completion-truthfulness rule, why this table is NOT in backend/scripts/
    s1a_privilege_policy.py's narrow-privilege allowlist).
    """

    __tablename__ = "memory_source_backfill_runs"
    __table_args__ = (
        CheckConstraint("batch_size > 0", name="ck_msbr_batch_size"),
        CheckConstraint(
            "total_candidates_snapshot >= 0 AND already_done_snapshot >= 0 "
            "AND processed_count >= 0 AND exact_chunk_count >= 0 "
            "AND degraded_version_count >= 0 AND missing_document_only_count >= 0 "
            "AND skipped_unresolvable_count >= 0 AND failed_count >= 0 "
            "AND batches_completed >= 0",
            name="ck_msbr_counters_non_negative",
        ),
    )
    __mapper_args__ = {"eager_defaults": False}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # native_enum=False + create_constraint=False: migration 0025 defines these as plain
    # varchar + CHECK, not a native Postgres ENUM TYPE — see storage_deletion_task.py's
    # identical, already-reviewed reasoning (Pass 27).
    mode: Mapped[BackfillRunMode] = mapped_column(Enum(BackfillRunMode, native_enum=False, create_constraint=False, length=16))
    status: Mapped[BackfillRunStatus] = mapped_column(
        Enum(BackfillRunStatus, native_enum=False, create_constraint=False, length=16),
        default=BackfillRunStatus.pending,
        index=True,
    )
    batch_size: Mapped[int] = mapped_column(Integer)
    # Point-in-time counts taken once at run creation — a progress denominator, not a
    # guarantee (see migration 0025's module docstring).
    total_candidates_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    already_done_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    exact_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    degraded_version_count: Mapped[int] = mapped_column(Integer, default=0)
    missing_document_only_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_unresolvable_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    batches_completed: Mapped[int] = mapped_column(Integer, default=0)
    # Resumable checkpoint — the newest (created_at, id) pair actually considered (in any
    # outcome) by the most recent advance_backfill_run() call. See migration 0025's module
    # docstring for why this is needed for BOTH modes, not just dry_run.
    last_cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cursor_claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MemorySourceBackfillFailure(Base):
    """One row per claim that failed to resolve or link during a specific run — a queryable,
    durable claim-level failure log (migration 0025). `reason` is the exact structural failure
    string app/rag/memory_source_backfill.py's `_resolve_locator()`/`_apply()` already produce
    (document/chunk/version/owner ids only — NEVER `KnowledgeClaim.claim_text`). Unique on
    (run_id, claim_id): a claim re-considered within the SAME run never produces a duplicate
    row for that run (attempt_count increments instead — see
    app/rag/memory_source_backfill_run.py's record_failure())."""

    __tablename__ = "memory_source_backfill_failures"
    __table_args__ = (
        UniqueConstraint("run_id", "claim_id", name="uq_msbf_run_claim"),
        CheckConstraint("attempt_count > 0", name="ck_msbf_attempt_count"),
    )
    __mapper_args__ = {"eager_defaults": False}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memory_source_backfill_runs.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_claims.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
