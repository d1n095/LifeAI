"""MainAI Runtime Truthfulness and Durable Job Foundation — see migration 0025's module
docstring for the full rationale and why this is a NEW table family, not a reuse of
`ImportJob` (Life Library-specific) or `AgentTask`/`AgentTaskEvent` (founder-wide, no
owner/lease/heartbeat/cancellation)."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAIJobStatus(str, enum.Enum):
    """A job's current lifecycle state. `paused` is reachable only via an explicit cancel
    request honored between batches (see app/rag/corpus_review_job.py) — there is no
    automatic pause. `queued`/`running` are the only claimable states (see
    CLAIMABLE_MAINAI_JOB_STATUSES below); every other state is terminal or requires an
    explicit retry to leave."""

    queued = "queued"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# claim_next_mainai_job() (app/jobs/mainai_job_lease.py) picks these up unconditionally by
# status; `running` is ALSO reclaimable, but only together with an expired lease (a time
# condition, not status membership), matching CLAIMABLE_IMPORT_JOB_STATUSES's own precedent
# (app/models/import_job.py) for why that split exists.
CLAIMABLE_MAINAI_JOB_STATUSES = frozenset({MainAIJobStatus.queued})

# A job can only ever be retried out of a genuinely terminal, non-cancelled state — retrying a
# `cancelled` job would silently override the founder's own cancellation, which
# app/rag/mainai_jobs_service.py's retry_job() treats as a hard error, not a no-op.
RETRYABLE_MAINAI_JOB_STATUSES = frozenset({MainAIJobStatus.failed})

# A cancel request is only meaningful while a job could still do further work.
CANCELLABLE_MAINAI_JOB_STATUSES = frozenset({MainAIJobStatus.queued, MainAIJobStatus.running, MainAIJobStatus.paused})


class MainAIJobEventType(str, enum.Enum):
    """One entry in a job's append-only execution-event history — application code only ever
    INSERTs here (see migration 0025's module docstring)."""

    created = "created"
    claimed = "claimed"
    heartbeat = "heartbeat"
    phase_changed = "phase_changed"
    progress_updated = "progress_updated"
    cancel_requested = "cancel_requested"
    cancel_acknowledged = "cancel_acknowledged"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    retry_scheduled = "retry_scheduled"
    # migration 0028 (founder re-review round, PR #36): one per-document outcome that was
    # NOT a successful review — corpus_review_job.py records this instead of silently folding
    # "deleted mid-run" / "no reviewable content" / "provider failed for this one document"
    # into a bare progress-count increment. `detail` always carries a closed-vocabulary
    # `reason` (see corpus_review_job.py's _SkipReason), never raw exception text.
    document_skipped = "document_skipped"


class MainAIJobProposalStatus(str, enum.Enum):
    proposed = "proposed"
    dismissed = "dismissed"


class MainAIJob(Base):
    """One durable, owner-scoped MainAI runtime job. See migration 0025's module docstring
    for the full column rationale and the founder's original requirement list this maps to.

    `public_message` is a bounded-length (varchar(500)), human-safe status string only —
    never raw exception text, a stack trace, or any internal identifier that could leak
    implementation detail; see app/rag/mainai_jobs_service.py's `_PUBLIC_ERROR_MESSAGES` for
    the fixed, reviewed mapping from `error_category` to what a caller actually sees.
    `error_category` is a small, closed vocabulary (see MainAIJobErrorCategory below), not a
    free-text field, for the same reason.
    """

    __tablename__ = "mainai_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[MainAIJobStatus] = mapped_column(Enum(MainAIJobStatus), default=MainAIJobStatus.queued)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    # None until the job itself can measure a total (e.g. after listing how many chunks a
    # corpus_review job will process) — the API surfaces this as "percentage not yet known"
    # rather than a misleading 0%.
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)

    # Artifact references: [{"type": "document", "id": "..."}] — never raw content, only
    # pointers to existing rows the owner's own RLS already governs.
    input_refs: Mapped[list] = mapped_column(JSON, default=list)
    output_refs: Mapped[list] = mapped_column(JSON, default=list)

    # Worker claim/lease — same Postgres FOR UPDATE SKIP LOCKED + lease-TTL pattern as
    # ImportJob's locked_by/lease_expires_at (app/jobs/lease.py), reused for a DIFFERENT table
    # via app/jobs/mainai_job_lease.py since the two job families have unrelated columns.
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Lease fencing token (migration 0028, founder re-review round on PR #36): bumped by
    # exactly 1 on every claim AND every reclaim (app/jobs/mainai_job_lease.py). worker_id
    # alone is not a safe fencing key -- app/worker.py's _worker_id() can return the SAME
    # value across a process restart (hostname-based), so a worker_id-only check can't tell a
    # genuinely stale execution from a legitimately restarted one reusing that identity. Every
    # worker-driven mutation of a claimed job (app/rag/mainai_jobs_service.py) must present
    # BOTH worker_id and this exact generation number, or it is rejected with
    # JobLeaseLostError -- see that module's docstring for the full incident this closes.
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)

    # Exact execution attribution — the founder's explicit "exact worker/model/tool
    # attribution" requirement. `locked_by` above already carries the worker; provider/model
    # carry which AI backend actually ran (mirrors AgentTaskEvent.provider/model).
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Who/what asked for this job — 'founder' for a direct request, or a future system
    # trigger name. Never a free-text field an end user controls the content of.
    created_by: Mapped[str] = mapped_column(String(64))


class MainAIJobEvent(Base):
    """One append-only step in a job's execution history. `detail` holds event-specific
    structured data (e.g. `{"phase": "extracting"}` for `phase_changed`, `{"error_category":
    ...}` for `failed`) — never raw exception text or content, same rule as
    MainAIJob.public_message above."""

    __tablename__ = "mainai_job_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[MainAIJobEventType] = mapped_column(Enum(MainAIJobEventType))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MainAIJobProposal(Base):
    """One extracted proposal from a `corpus_review` job (app/rag/corpus_review_job.py) —
    deliberately NOT a KnowledgeClaim (see migration 0025's module docstring): nothing in this
    codebase auto-promotes a row here into founder-approved truth. `source_chunk_id` is
    nullable because a document with no chunks yet (see app/models/document.py) can still be
    reviewed at the whole-document level."""

    __tablename__ = "mainai_job_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True
    )
    proposal_type: Mapped[str] = mapped_column(String(32))
    proposal_text: Mapped[str] = mapped_column(Text)
    status: Mapped[MainAIJobProposalStatus] = mapped_column(Enum(MainAIJobProposalStatus), default=MainAIJobProposalStatus.proposed)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MainAIJobErrorCategory(str, enum.Enum):
    """Closed vocabulary for MainAIJob.error_category — a small, reviewed set safe to show
    (via a fixed public-message mapping, see app/rag/mainai_jobs_service.py) directly to a
    caller, never the underlying exception itself."""

    transient_io = "transient_io"
    permanent = "permanent"
    capability_unavailable = "capability_unavailable"
    cancelled_by_owner = "cancelled_by_owner"
    timeout = "timeout"
    unexpected = "unexpected"
