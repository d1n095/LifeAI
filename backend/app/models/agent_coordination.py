"""Multi-Agent Work Coordination foundation — see migration 0046's module docstring for the
full column rationale and docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md for the architecture.

This is a coordination layer ABOVE the existing canonical `mainai_goals`/`mainai_tasks`
(app/models/mainai_execution.py) — it assigns external coding agents (Claude Code, Cursor
Agent, Codex, ...) to already-authorized goals/tasks and tracks the branch/worktree/path
scope each assignment holds; it never mints a second task/job queue, a second approval
system, or a second capability-learning system. Capability/performance evidence is recorded
through the existing app.intelligence_governance primitives (IntelligenceExecution/
IntelligenceEvidence), referenced here by id, never duplicated."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CoordinationAgentStatus(str, enum.Enum):
    active = "active"
    unavailable = "unavailable"
    disabled = "disabled"


class AgentAdapterKind(str, enum.Enum):
    """What KIND of channel this agent is reached through — never itself a credential or a
    live connection; see docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's AgentAdapter section
    for the (not-yet-implemented) Protocol this foundation only defines the shape of."""

    cli = "cli"
    api = "api"
    internal = "internal"


class WorkAssignmentRole(str, enum.Enum):
    """A REVIEWER (or RESEARCHER) must be assignable strictly READ_ONLY — enforced by
    `read_write_mode`, not by role alone (an agent's role names its INTENDED job; the actual
    write authority is always the assignment's own `read_write_mode` + its lease)."""

    builder = "builder"
    reviewer = "reviewer"
    tester = "tester"
    challenger = "challenger"
    researcher = "researcher"
    synthesizer = "synthesizer"


class WorkAssignmentReadWriteMode(str, enum.Enum):
    read_only = "read_only"
    read_write = "read_write"


class WorkAssignmentStatus(str, enum.Enum):
    planned = "planned"
    ready = "ready"
    running = "running"
    waiting_dependency = "waiting_dependency"
    waiting_agent = "waiting_agent"
    waiting_review = "waiting_review"
    blocked = "blocked"
    ready_for_review = "ready_for_review"
    reviewing = "reviewing"
    changes_requested = "changes_requested"
    verified = "verified"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    superseded = "superseded"


TERMINAL_WORK_ASSIGNMENT_STATUSES = frozenset(
    {
        WorkAssignmentStatus.completed,
        WorkAssignmentStatus.failed,
        WorkAssignmentStatus.cancelled,
        WorkAssignmentStatus.superseded,
    }
)


class AgentScopeLeaseStatus(str, enum.Enum):
    active = "active"
    released = "released"
    superseded = "superseded"


class ParallelExplorationGroupStatus(str, enum.Enum):
    open = "open"
    synthesizing = "synthesizing"
    resolved = "resolved"
    abandoned = "abandoned"


class CoordinationAgent(Base):
    """One entry in the provider-neutral agent registry. Deliberately NOT owner-scoped/RLS
    -protected — which coding agents/tools exist is founder-wide system knowledge, the same
    reasoning app.models.mainai_execution.EngineeringLesson already documents for itself, not
    per-owner personal data. Never stores a credential (see this table's own CHECK constraint
    in migration 0046 -- `model_hint` is an informational label like "claude-sonnet-5", never
    a key)."""

    __tablename__ = "coordination_agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    adapter_kind: Mapped[AgentAdapterKind] = mapped_column(Enum(AgentAdapterKind))
    # Closed-vocabulary capability tags this coordinator understands (e.g. "repo_edit",
    # "read_only_review", "run_tests") -- descriptive metadata only; never itself a grant of
    # write authority (that is always the assignment's own read_write_mode + its lease).
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    execution_mode: Mapped[str] = mapped_column(String(32), default="unknown")
    supports_read: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_write: Mapped[bool] = mapped_column(Boolean, default=False)
    # Informational only (e.g. "claude-sonnet-5") -- never a credential; see module docstring.
    model_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cost_class: Mapped[str] = mapped_column(String(16), default="unknown")
    concurrency_limit: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[CoordinationAgentStatus] = mapped_column(Enum(CoordinationAgentStatus), default=CoordinationAgentStatus.active)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ParallelExplorationGroup(Base):
    """Groups multiple independent WORK ASSIGNMENTS solving the SAME canonical problem in
    ISOLATED branches/worktrees -- the explicit, non-accidental form of "two builders on the
    same problem" the founder's PARALLEL_EXPLORATION mode requires. Resolution
    (`synthesis_result_ref`) is always filled in by an explicit later action (a founder or a
    future synthesis primitive) -- this table never auto-merges or auto-selects a winner."""

    __tablename__ = "parallel_exploration_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # What all member assignments are independently trying to solve -- a human-assigned
    # identity string (e.g. a LifeProblem ref, or a short slug), never inferred.
    canonical_problem_ref: Mapped[str] = mapped_column(String(256))
    status: Mapped[ParallelExplorationGroupStatus] = mapped_column(
        Enum(ParallelExplorationGroupStatus), default=ParallelExplorationGroupStatus.open
    )
    synthesis_result_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="CASCADE"),
    )


class AgentWorkAssignment(Base):
    """One durable, owner-scoped agent job: WHO (agent_id) is doing WHAT (goal_id/task_id),
    WHERE (repository_identity/branch/worktree_path/allowed_paths), and with WHAT authority
    (read_write_mode, risk_level, approval_required). This table never dispatches a
    `mainai_jobs` row or bypasses `app.mainai_execution.approval.require_task_approval()` --
    `approval_required` here is coordination-level descriptive metadata about the eventual
    write this assignment is expected to produce, not a second enforcement gate; the real gate
    remains exactly where it already is, unchanged.

    `canonical_work_key` is the deterministic identity used for duplicate-work detection (see
    app.agent_coordination.service.is_duplicate_canonical_work) -- a plain function of
    (goal_id, task_id, role), never guessed."""

    __tablename__ = "agent_work_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("coordination_agents.id"), index=True)
    role: Mapped[WorkAssignmentRole] = mapped_column(Enum(WorkAssignmentRole))
    read_write_mode: Mapped[WorkAssignmentReadWriteMode] = mapped_column(Enum(WorkAssignmentReadWriteMode))
    repository_identity: Mapped[str] = mapped_column(String(512))
    # Path-prefix scope (e.g. "backend/app/finance/**", or an exact file path) -- the ONLY
    # input app.agent_coordination.service.paths_conflict() reasons over; never natural
    # language.
    allowed_paths: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[WorkAssignmentStatus] = mapped_column(Enum(WorkAssignmentStatus), default=WorkAssignmentStatus.planned)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upstream_pr_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="low")
    approval_required: Mapped[bool] = mapped_column(Boolean, default=False)
    parallel_exploration_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    canonical_work_key: Mapped[str] = mapped_column(String(256), index=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    output_refs: Mapped[list] = mapped_column(JSON, default=list)
    intelligence_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["goal_id", "owner_id"], ["mainai_goals.id", "mainai_goals.owner_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["task_id", "owner_id"], ["mainai_tasks.id", "mainai_tasks.owner_id"], ondelete="SET NULL"),
        ForeignKeyConstraint(
            ["parallel_exploration_group_id", "owner_id"],
            ["parallel_exploration_groups.id", "parallel_exploration_groups.owner_id"],
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["intelligence_execution_id", "owner_id"],
            ["intelligence_executions.id", "intelligence_executions.owner_id"],
            ondelete="SET NULL",
        ),
    )


class AgentWorkAssignmentDependency(Base):
    """One edge: `assignment_id` should not be considered READY until `depends_on_assignment_id`
    reaches a qualifying state (see app.agent_coordination.service's dependency-release rule).
    Mirrors app.models.mainai_execution.MainAITaskDependency's exact shape."""

    __tablename__ = "agent_work_assignment_dependencies"

    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    depends_on_assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "owner_id"], ["agent_work_assignments.id", "agent_work_assignments.owner_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["depends_on_assignment_id", "owner_id"],
            ["agent_work_assignments.id", "agent_work_assignments.owner_id"],
            ondelete="CASCADE",
        ),
    )


class AgentScopeLease(Base):
    """The deterministic write-conflict-prevention primitive. `lease_generation` follows the
    EXACT fencing convention app.models.mainai_job.MainAIJob.lease_generation already
    establishes (bumped by exactly 1 on every takeover; a caller must present the exact
    current generation to mutate/renew/release, never worker/agent identity alone -- see that
    model's own docstring for the incident this pattern closes). One row per currently-held
    scope; a takeover bumps THIS row's generation in place (does not create a new row), so a
    holder's own stale reference to an old generation number is naturally fenced out."""

    __tablename__ = "agent_scope_leases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("coordination_agents.id"), index=True)
    repository_identity: Mapped[str] = mapped_column(String(512), index=True)
    branch: Mapped[str] = mapped_column(String(255))
    worktree_path: Mapped[str] = mapped_column(Text)
    allowed_paths: Mapped[list] = mapped_column(JSON, default=list)
    mode: Mapped[WorkAssignmentReadWriteMode] = mapped_column(Enum(WorkAssignmentReadWriteMode))
    lease_generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[AgentScopeLeaseStatus] = mapped_column(Enum(AgentScopeLeaseStatus), default=AgentScopeLeaseStatus.active)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # NULL == held until explicit release (no TTL) -- a caller MAY set a TTL and renew via
    # heartbeat_lease(); staleness is always a timestamp comparison at read time, never a
    # separate stored status value, matching MainAIJob.lease_expires_at's own convention.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "owner_id"], ["agent_work_assignments.id", "agent_work_assignments.owner_id"], ondelete="CASCADE"
        ),
    )


class AgentWorkAssignmentEventType(str, enum.Enum):
    created = "created"
    agent_assigned = "agent_assigned"
    lease_acquired = "lease_acquired"
    lease_renewed = "lease_renewed"
    lease_taken_over = "lease_taken_over"
    lease_released = "lease_released"
    status_changed = "status_changed"
    dependency_satisfied = "dependency_satisfied"
    evidence_recorded = "evidence_recorded"
    conflict_detected = "conflict_detected"


class AgentWorkAssignmentEvent(Base):
    """One append-only step in an assignment's history -- application code only ever INSERTs
    here (migration 0046's DB trigger enforces this), same pattern as MainAITaskEvent."""

    __tablename__ = "agent_work_assignment_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[AgentWorkAssignmentEventType] = mapped_column(Enum(AgentWorkAssignmentEventType))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "owner_id"], ["agent_work_assignments.id", "agent_work_assignments.owner_id"], ondelete="CASCADE"
        ),
    )
