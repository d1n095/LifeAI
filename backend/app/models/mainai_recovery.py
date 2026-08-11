"""MainAI V0.2 — Dead Agent Recovery durable schema. See migration 0033's module docstring for
the full rationale, including the two architecture facts (no pre-existing worktree isolation,
no persistent worker filesystem) that drove this schema's shape."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAITaskWorktreeStatus(str, enum.Enum):
    active = "active"
    released = "released"
    abandoned = "abandoned"


class MainAITaskWorktreeRecoveryState(str, enum.Enum):
    """Whether a recovery pass has verified this worktree's ownership -- separate from
    MainAITaskWorktreeStatus, which tracks the worktree's own filesystem lifecycle, not
    whether a takeover is allowed to reuse it."""

    unclaimed = "unclaimed"
    ownership_verified = "ownership_verified"
    ownership_rejected = "ownership_rejected"
    claimed_for_takeover = "claimed_for_takeover"


# A working branch must never be one of these -- enforced both here (defense in depth) and by
# migration 0033's ck_mainai_task_worktrees_branch_not_protected CHECK constraint.
PROTECTED_BRANCHES = frozenset({"main", "master", "claude/det-kommer-mer-879lcm"})


class MainAIRecoveryStatus(str, enum.Enum):
    """A recovery record's own lifecycle -- distinct from MainAITaskStatus (the task it's
    recovering) and MainAIJobStatus (the dead job it's recovering from)."""

    detected = "detected"
    inspecting = "inspecting"
    inspected = "inspected"
    classified = "classified"
    salvaging = "salvaging"
    salvaged = "salvaged"
    taking_over = "taking_over"
    taken_over = "taken_over"
    blocked = "blocked"
    completed = "completed"


class RecoveryClassification(str, enum.Enum):
    """The founder's own A-I vocabulary. See migration 0033 and
    docs/MAINAI_DEAD_AGENT_RECOVERY_V0_2.md for the exact evidence each code maps to."""

    nothing_done = "NOTHING_DONE"
    checkpointed_work = "CHECKPOINTED_WORK"
    local_uncommitted_work = "LOCAL_UNCOMMITTED_WORK"
    local_committed_not_pushed = "LOCAL_COMMITTED_NOT_PUSHED"
    pushed_no_pr = "PUSHED_NO_PR"
    pr_exists = "PR_EXISTS"
    verified_work = "VERIFIED_WORK"
    conflicted_state = "CONFLICTED_STATE"
    unsafe_to_auto_recover = "UNSAFE_TO_AUTO_RECOVER"


# A recovery record may only ever be auto-continued (salvage/takeover attempted without
# founder approval) from these classifications -- matches the founder's explicit "V0.2 ska
# helst inte implementera force/destructive recovery alls. Fail closed istället." Anything
# NOT in this set (CONFLICTED_STATE, UNSAFE_TO_AUTO_RECOVER) always sets
# manual_review_required=True and stops there. See app/mainai_execution/recovery_classifier.py.
AUTO_SALVAGEABLE_CLASSIFICATIONS = frozenset(
    {
        RecoveryClassification.nothing_done,
        RecoveryClassification.checkpointed_work,
        RecoveryClassification.local_uncommitted_work,
        RecoveryClassification.local_committed_not_pushed,
        RecoveryClassification.pushed_no_pr,
        RecoveryClassification.pr_exists,
        RecoveryClassification.verified_work,
    }
)


class MainAIRecoveryEventType(str, enum.Enum):
    """One entry in a recovery record's append-only history -- migration 0033's DB trigger
    enforces this, same pattern as MainAITaskEvent."""

    stalled_detected = "stalled_detected"
    dead_detected = "dead_detected"
    recovery_started = "recovery_started"
    recovery_inspected = "recovery_inspected"
    recovery_classified = "recovery_classified"
    salvage_started = "salvage_started"
    salvage_completed = "salvage_completed"
    takeover_started = "takeover_started"
    takeover_completed = "takeover_completed"
    recovery_blocked = "recovery_blocked"
    manual_review_required = "manual_review_required"


class MainAITaskWorktree(Base):
    """One isolated, per-(task, job/attempt) local filesystem checkout -- see migration 0033's
    module docstring for why this did not already exist in V0.1 and why it must never be
    trusted by path/task_id alone (`marker_token` is the actual trust anchor, verified by
    reading it back from the on-disk marker file, see
    app/mainai_execution/worktree.py's `verify_worktree_ownership()`)."""

    __tablename__ = "mainai_task_worktrees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), unique=True)
    lease_generation: Mapped[int] = mapped_column(Integer)
    executor_id: Mapped[str] = mapped_column(String(128))
    repo: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(Text)
    marker_token: Mapped[str] = mapped_column(String(64))
    current_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovery_state: Mapped[MainAITaskWorktreeRecoveryState] = mapped_column(
        Enum(MainAITaskWorktreeRecoveryState), default=MainAITaskWorktreeRecoveryState.unclaimed
    )
    status: Mapped[MainAITaskWorktreeStatus] = mapped_column(Enum(MainAITaskWorktreeStatus), default=MainAITaskWorktreeStatus.active)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MainAIRecoveryRecord(Base):
    """One durable recovery attempt for one dead/stalled `mainai_jobs` row. See migration
    0033's module docstring for the full column rationale."""

    __tablename__ = "mainai_recovery_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="CASCADE"), unique=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime)
    classification: Mapped[RecoveryClassification | None] = mapped_column(Enum(RecoveryClassification), nullable=True)
    # Structured, durable evidence snapshot (checkpoint/worktree/branch/commit/PR/CI state) --
    # never a summary reconstructed from memory later. See recovery_inspector.py.
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    salvage_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    takeover_executor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    takeover_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[MainAIRecoveryStatus] = mapped_column(Enum(MainAIRecoveryStatus), default=MainAIRecoveryStatus.detected)
    blocker: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MainAIRecoveryEvent(Base):
    """One append-only step in a recovery record's history. `detail` holds event-specific
    structured data -- never raw exception text, same rule as MainAITaskEvent.detail."""

    __tablename__ = "mainai_recovery_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recovery_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_recovery_records.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[MainAIRecoveryEventType] = mapped_column(Enum(MainAIRecoveryEventType))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
