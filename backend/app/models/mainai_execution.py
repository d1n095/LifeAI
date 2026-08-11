"""MainAI Execution Loop V0.1 — Goal/Plan/Task/Checkpoint durable schema, plus the engineering
lesson (safety memory) foundation. See migration 0032's module docstring for the full
rationale, including why this is a NEW table family sitting ABOVE the existing `mainai_jobs`
runtime rather than a parallel lease/heartbeat system."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class MainAIGoalStatus(str, enum.Enum):
    """Richer than MainAIJobStatus (app/models/mainai_job.py) because a goal's lifecycle spans
    planning AND execution, not just one queued unit of work. `waiting` covers both
    waiting_external and waiting_ci at the goal level (a goal is "waiting" if ANY of its
    in-flight tasks is); `blocked` means the planner cannot proceed without founder input."""

    pending = "pending"
    planning = "planning"
    running = "running"
    waiting = "waiting"
    blocked = "blocked"
    failed = "failed"
    completed = "completed"
    cancelled = "cancelled"


# A goal is only ever considered "live" (worth polling/showing as active) in these states.
ACTIVE_MAINAI_GOAL_STATUSES = frozenset(
    {MainAIGoalStatus.pending, MainAIGoalStatus.planning, MainAIGoalStatus.running, MainAIGoalStatus.waiting, MainAIGoalStatus.blocked}
)
TERMINAL_MAINAI_GOAL_STATUSES = frozenset({MainAIGoalStatus.failed, MainAIGoalStatus.completed, MainAIGoalStatus.cancelled})


class MainAIGoalRiskLevel(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"


class MainAIPlanStatus(str, enum.Enum):
    """Only one `active` plan per goal at a time — enforced in the service layer (see
    app/mainai_execution/planner.py), not a DB constraint, since "supersede the previous
    active plan" is a two-row transition no single-row CHECK can express."""

    draft = "draft"
    active = "active"
    superseded = "superseded"


class MainAITaskStatus(str, enum.Enum):
    """Richer than MainAIJobStatus for the same reason MainAIGoalStatus is: a task can be
    blocked on a dependency, waiting on an external system, or waiting on CI, none of which
    map onto a bare job's queued/running/paused/completed/failed/cancelled vocabulary."""

    pending = "pending"
    ready = "ready"
    running = "running"
    waiting_external = "waiting_external"
    waiting_ci = "waiting_ci"
    blocked = "blocked"
    retryable_failed = "retryable_failed"
    failed = "failed"
    completed = "completed"
    cancelled = "cancelled"


# A task may only be selected for dispatch (see app/mainai_execution/graph.py) from this set.
READY_FOR_DISPATCH_MAINAI_TASK_STATUSES = frozenset({MainAITaskStatus.ready})
TERMINAL_MAINAI_TASK_STATUSES = frozenset({MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled})
# A task can only be retried out of a genuinely retryable-terminal state — never `cancelled`,
# for the identical reason RETRYABLE_MAINAI_JOB_STATUSES excludes it (app/models/mainai_job.py).
RETRYABLE_MAINAI_TASK_STATUSES = frozenset({MainAITaskStatus.retryable_failed})


class MainAITaskEventType(str, enum.Enum):
    """One entry in a task's append-only execution-event history — application code only ever
    INSERTs here (migration 0032's DB trigger enforces this, same pattern as
    MainAIJobEvent)."""

    created = "created"
    ready = "ready"
    dispatched = "dispatched"
    progress_updated = "progress_updated"
    verification_started = "verification_started"
    verification_passed = "verification_passed"
    verification_failed = "verification_failed"
    blocked = "blocked"
    replanned = "replanned"
    retry_scheduled = "retry_scheduled"
    approval_required = "approval_required"
    approval_granted = "approval_granted"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class MainAIGoal(Base):
    """One durable, owner-scoped MainAI execution run. See migration 0032's module docstring
    for the full column rationale."""

    __tablename__ = "mainai_goals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    original_instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[MainAIGoalStatus] = mapped_column(Enum(MainAIGoalStatus), default=MainAIGoalStatus.pending)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_plan_version: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[MainAIGoalRiskLevel] = mapped_column(Enum(MainAIGoalRiskLevel), default=MainAIGoalRiskLevel.low)
    # Names a policy defined in app/mainai_execution/approval.py's APPROVAL_POLICIES registry —
    # never inline logic stored as data (see that module for why).
    approval_policy: Mapped[str] = mapped_column(String(32), default="standard_repo_work")
    final_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))


class MainAIPlan(Base):
    """One versioned, append-only breakdown of a goal into tasks. A replan creates a NEW row
    (version = previous max + 1); an earlier version's rows are never edited or deleted."""

    __tablename__ = "mainai_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[MainAIPlanStatus] = mapped_column(Enum(MainAIPlanStatus), default=MainAIPlanStatus.draft)
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(64))


class MainAITask(Base):
    """One node in a goal's task graph. `mainai_job_id` points at the real, leased/heartbeated
    `mainai_jobs` row (app/models/mainai_job.py) currently or most recently executing this
    task — liveness/retry/cancellation of the actual work is governed entirely by that
    already-reviewed runtime, not reimplemented here."""

    __tablename__ = "mainai_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_plans.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(Text)
    # A capability name the executor dispatch table (app/mainai_execution/executor.py)
    # recognizes — e.g. "read_only_audit", "repo_edit", "run_tests", "open_pr". Unknown values
    # are rejected the same fail-closed way app/mainai_runtime_contract.py's
    # CAPABILITY_MANIFEST rejects an unknown mainai_jobs job_type.
    task_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[MainAITaskStatus] = mapped_column(Enum(MainAITaskStatus), default=MainAITaskStatus.pending)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[MainAIGoalRiskLevel] = mapped_column(Enum(MainAIGoalRiskLevel), default=MainAIGoalRiskLevel.low)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    # Structured, closed-vocabulary verification steps (e.g. [{"kind": "targeted_tests",
    # "target": "tests/backend/..."}]) — never free text the executor could reinterpret as
    # "verification passed" on its own say-so. See app/mainai_execution/verify.py.
    verification_plan: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    blocker_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    mainai_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MainAITaskDependency(Base):
    """One edge of a goal's task graph: `task_id` cannot become `ready` until
    `depends_on_task_id` is `completed` (see app/mainai_execution/graph.py's readiness
    check). A DB CHECK denies a literal self-edge; multi-row cycles are rejected at
    plan-creation time by the planner's own DFS (a single-row CHECK cannot express a cycle)."""

    __tablename__ = "mainai_task_dependencies"

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_tasks.id", ondelete="CASCADE"), primary_key=True)
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_tasks.id", ondelete="CASCADE"), primary_key=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MainAITaskEvent(Base):
    """One append-only step in a task's execution history. `detail` holds event-specific
    structured data — never raw exception text or content, same rule as
    MainAIJobEvent.detail (app/models/mainai_job.py)."""

    __tablename__ = "mainai_task_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_tasks.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[MainAITaskEventType] = mapped_column(Enum(MainAITaskEventType))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MainAICheckpoint(Base):
    """One durable, append-only resume point. Resume always reads the LATEST checkpoint row
    for a goal (see app/mainai_execution/checkpoint.py) — an old row is never mutated, only
    superseded by a newer insert, same append-only philosophy as MainAITaskEvent above."""

    __tablename__ = "mainai_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    # branch/worktree/commit SHA/last-completed-step/pending-step — structured, closed shape
    # documented in app/mainai_execution/checkpoint.py, never free text.
    executor_state: Mapped[dict] = mapped_column(JSON, default=dict)
    test_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ci_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    blocker: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class EngineeringLessonStatus(str, enum.Enum):
    """Reuses the active/historical/superseded shape ActiveTruthStatus (app/models/document.py)
    and ClaimStatus (app/models/knowledge_claim.py) already establish, rather than inventing a
    fourth vocabulary for "is this still true.\""""

    active = "active"
    historical = "historical"
    superseded = "superseded"


class EngineeringLessonSeverity(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class EngineeringLessonConfidence(str, enum.Enum):
    """Deliberately smaller than ClaimConfidence (app/models/knowledge_claim.py) — a lesson has
    no `conflict`/`no_basis` state today because nothing yet computes lesson-vs-lesson
    contradiction the way assess_claim_confidence() does for claims; see
    docs/MAINAI_EXECUTION_LOOP_V0_1.md's REAL/STUBBED/LIMITED list for this explicitly named
    limitation."""

    certain = "certain"
    likely = "likely"
    uncertain = "uncertain"


class EngineeringLesson(Base):
    """One founder-wide engineering/safety lesson with mandatory provenance — the machine
    readable counterpart to what has, until now, only ever lived as prose in
    docs/BRANCH_REGISTRY.md's `## Pass N` sections. Follows ProjectNote's exact provenance
    shape (app/models/project_memory.py: source_type/source_ref, never a bare unsourced
    claim). Deliberately NOT RLS-protected — founder-wide project/system knowledge, not
    per-owner personal data, same reasoning as ProjectNote itself. Deliberately its OWN table,
    not a KnowledgeClaim row: the fields here don't fit a single claim_text column, and nothing
    in this codebase auto-promotes a lesson into founder-approved truth either (same
    non-promotion rule mainai_job_proposals already establishes)."""

    __tablename__ = "engineering_lessons"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[EngineeringLessonStatus] = mapped_column(Enum(EngineeringLessonStatus), default=EngineeringLessonStatus.active)
    problem: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    affected_component: Mapped[str] = mapped_column(String(128), index=True)
    severity: Mapped[EngineeringLessonSeverity] = mapped_column(Enum(EngineeringLessonSeverity))
    evidence: Mapped[str] = mapped_column(Text)
    fix: Mapped[str] = mapped_column(Text)
    regression_test: Mapped[str | None] = mapped_column(String(256), nullable=True)
    general_rule: Mapped[str] = mapped_column(Text)
    # Tags/patterns a planner/verifier can match against (e.g. ["lease", "worker",
    # "privilege_boot"]) — see app/mainai_execution/lessons.py's lookup function.
    applies_to: Mapped[list] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str] = mapped_column(String(256))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_in: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confidence: Mapped[EngineeringLessonConfidence] = mapped_column(Enum(EngineeringLessonConfidence), default=EngineeringLessonConfidence.likely)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("engineering_lessons.id", ondelete="SET NULL"), nullable=True
    )
