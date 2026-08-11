"""MainAI V0.3 — Long-Running Orchestration: durable external-wait tracking. See migration
0036's module docstring for the full rationale, including why this is a NEW, general-but-
minimal table rather than a github-specific one, and why no new `mainai_tasks`/`mainai_jobs`
lease/fencing column was needed to build it."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAITaskWaitSourceType(str, enum.Enum):
    """Closed vocabulary for what a wait is actually watching. Only `github_check_runs` is
    implemented in V0.3 (app/mainai_execution/ci_wait.py) — the column exists as a real,
    CHECK-enforced closed vocabulary (not a free-text field) specifically so a second source
    could be added later without a schema change, matching the founder's explicit "general but
    minimal" instruction; it is not itself evidence of a second source actually existing."""

    github_check_runs = "github_check_runs"


class MainAITaskWaitStatus(str, enum.Enum):
    """A wait record's own lifecycle -- distinct from MainAITaskStatus (the task waiting) and
    MainAIJobStatus (the job that created this wait)."""

    pending = "pending"
    satisfied = "satisfied"
    failed = "failed"
    timed_out = "timed_out"
    cancelled = "cancelled"


# A wait may only ever be polled while still `pending` -- every other status is terminal.
TERMINAL_MAINAI_TASK_WAIT_STATUSES = frozenset(
    {MainAITaskWaitStatus.satisfied, MainAITaskWaitStatus.failed, MainAITaskWaitStatus.timed_out, MainAITaskWaitStatus.cancelled}
)


class MainAITaskWait(Base):
    """One durable, owner-scoped external-wait record -- what a `waiting_ci`/`waiting_external`
    MainAITask is actually waiting for, evaluated by whom, and when to poll again. `UNIQUE(job_id)`
    matches `mainai_task_worktrees`/`mainai_recovery_records`'s own precedent (migration 0033):
    one job attempt creates at most one wait. `deadline_at` is NOT NULL -- every wait has a real
    timeout (see app/mainai_execution/wait_poll.py), so a task can never wait forever."""

    __tablename__ = "mainai_task_waits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), unique=True)
    source_type: Mapped[MainAITaskWaitSourceType] = mapped_column(
        Enum(MainAITaskWaitSourceType, values_callable=lambda enum_cls: [m.value for m in enum_cls])
    )
    # {"repo": "...", "sha": "..."} for github_check_runs -- never trusted by task_id alone;
    # ci_wait.py always polls the EXACT sha recorded here, see that module's own docstring for
    # why a stale-SHA poll would be a real correctness bug, not just an inefficiency.
    resource_ref: Mapped[dict] = mapped_column(JSON, default=dict)
    # Evaluator config -- {} means "every check run must conclude success", the only condition
    # V0.3 implements (see ci_wait.py's evaluate_check_runs()).
    condition: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[MainAITaskWaitStatus] = mapped_column(
        Enum(MainAITaskWaitStatus, values_callable=lambda enum_cls: [m.value for m in enum_cls]), default=MainAITaskWaitStatus.pending
    )
    poll_count: Mapped[int] = mapped_column(Integer, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_poll_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    backoff_seconds: Mapped[int] = mapped_column(Integer, default=30)
    deadline_at: Mapped[datetime] = mapped_column(DateTime)
    # Last poll's raw (but never unbounded) evidence -- e.g. the check_runs summary that decided
    # the outcome. Never a full unbounded GitHub API response.
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
