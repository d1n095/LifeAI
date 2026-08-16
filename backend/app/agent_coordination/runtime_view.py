"""Agent Runtime Control Plane -- read-only visibility layer over the tables migration 0046
already created (`app.models.agent_coordination`). Answers "what is every registered agent
doing RIGHT NOW" and "who is doing what, where, with what authority" as a single deterministic
snapshot -- never a new store, never new tables, never a second registry. Every field here is
computed from `CoordinationAgent`/`AgentWorkAssignment`/`AgentScopeLease` at read time; nothing
is cached or duplicated.

See docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's "Runtime visibility & deterministic routing"
section for the full mapping rationale.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.service import (
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
    AgentScopeLeaseStatus,
    CoordinatorDecision,
    agent_availability,
    evaluate_assignment_readiness,
)
from app.models.agent_coordination import (
    AgentScopeLease,
    AgentWorkAssignment,
    CoordinationAgent,
    CoordinationAgentStatus,
    WorkAssignmentStatus,
)


class RuntimeStatus(str, enum.Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    WAITING_REVIEW = "WAITING_REVIEW"
    REVIEWING = "REVIEWING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"


# Deterministic, total mapping from the canonical WorkAssignmentStatus (the real, durable
# state machine app.agent_coordination.service.transition_status enforces) to this read-only
# layer's coarser founder-facing vocabulary. Never the other way around -- this enum is a
# VIEW; WorkAssignmentStatus remains the one canonical source of truth, verbatim-preserved
# alongside it on every AssignmentRuntimeView (see `canonical_status` below).
_ASSIGNMENT_RUNTIME_STATUS: dict[WorkAssignmentStatus, RuntimeStatus] = {
    WorkAssignmentStatus.planned: RuntimeStatus.IDLE,
    WorkAssignmentStatus.ready: RuntimeStatus.IDLE,
    # "waiting_agent" means the work is ready and allocated but this agent has not yet picked
    # it up -- it is not yet occupying the agent's attention (see
    # app.agent_coordination.service.CONCURRENCY_CONSUMING_STATUSES's own identical
    # "queued vs claimed" distinction), so it contributes IDLE, not a busy status.
    WorkAssignmentStatus.waiting_agent: RuntimeStatus.IDLE,
    WorkAssignmentStatus.running: RuntimeStatus.RUNNING,
    WorkAssignmentStatus.waiting_dependency: RuntimeStatus.WAITING_DEPENDENCY,
    WorkAssignmentStatus.waiting_review: RuntimeStatus.WAITING_REVIEW,
    WorkAssignmentStatus.ready_for_review: RuntimeStatus.WAITING_REVIEW,
    WorkAssignmentStatus.reviewing: RuntimeStatus.REVIEWING,
    WorkAssignmentStatus.blocked: RuntimeStatus.BLOCKED,
    WorkAssignmentStatus.changes_requested: RuntimeStatus.BLOCKED,
    # "verified" -- review passed, nothing further required of THIS agent -- grouped with
    # COMPLETED rather than inventing a tenth bucket the founder's own status list never asked
    # for; a verified-but-not-yet-completed assignment no longer occupies the agent.
    WorkAssignmentStatus.verified: RuntimeStatus.COMPLETED,
    WorkAssignmentStatus.completed: RuntimeStatus.COMPLETED,
    WorkAssignmentStatus.cancelled: RuntimeStatus.COMPLETED,
    WorkAssignmentStatus.superseded: RuntimeStatus.COMPLETED,
    WorkAssignmentStatus.failed: RuntimeStatus.FAILED,
}

# Agent-level overall status is the HIGHEST-priority runtime status across all of an agent's
# current non-terminal assignments (an agent's concurrency_limit MAY exceed 1) -- first match
# in this order wins. IDLE is the implicit default when no assignment matches anything above
# it (including when the agent has no assignments at all).
_AGENT_STATUS_PRIORITY: tuple[RuntimeStatus, ...] = (
    RuntimeStatus.RUNNING,
    RuntimeStatus.REVIEWING,
    RuntimeStatus.WAITING_REVIEW,
    RuntimeStatus.WAITING_DEPENDENCY,
    RuntimeStatus.BLOCKED,
)


@dataclass(frozen=True)
class AssignmentRuntimeView:
    """One assignment's complete WHO/WHAT/WHERE/AUTHORITY snapshot -- the "real work
    assignment registry" answer: who is doing what, where, with what write authority, on which
    branch, in which worktree, based on which sha, for which goal/task, with which
    dependency/wait reason blocking it right now."""

    assignment_id: uuid.UUID
    agent_id: uuid.UUID
    agent_key: str
    role: str
    runtime_status: RuntimeStatus
    canonical_status: str  # the underlying WorkAssignmentStatus value, verbatim -- never lossy
    read_write_mode: str
    repository_identity: str
    branch: str | None
    worktree_path: str | None
    base_sha: str | None
    head_sha: str | None
    allowed_paths: tuple[str, ...]
    goal_id: uuid.UUID
    task_id: uuid.UUID | None
    parallel_exploration_group_id: uuid.UUID | None
    upstream_pr_ref: str | None
    block_reason: str | None  # None when runtime_status carries no blocker (RUNNING/COMPLETED/...)
    block_outcome: str | None  # the live CoordinatorDecision.outcome code, e.g. "WAITING_DEPENDENCY"
    active_lease_id: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class AgentRuntimeView:
    """One agent's complete canonical-registry snapshot -- WHO exists, its static profile
    (provider/model/role capability/availability), and its derived overall runtime status plus
    every one of its currently non-terminal assignments. Nothing here is stored; it is
    recomputed from CoordinationAgent/AgentWorkAssignment/AgentScopeLease on every call."""

    agent_id: uuid.UUID
    agent_key: str
    display_name: str
    adapter_kind: str
    model_hint: str | None
    capabilities: tuple[str, ...]
    concurrency_limit: int
    registry_status: str  # the underlying CoordinationAgentStatus value -- active/unavailable/disabled
    runtime_status: RuntimeStatus
    availability_reason: str
    heartbeat_at: datetime | None  # most recent last_heartbeat_at across this agent's active leases, if any
    current_assignments: tuple[AssignmentRuntimeView, ...]


def _block_reason_for(db: Session, assignment: AgentWorkAssignment) -> tuple[str | None, str | None]:
    """(None, None) for a status that carries no blocker (RUNNING/COMPLETED/FAILED/...);
    otherwise the LIVE CoordinatorDecision -- deterministic, never guessed, never a stale
    stored value that could drift from what evaluate_assignment_readiness() would say right
    now if asked directly."""

    if assignment.status in TERMINAL_WORK_ASSIGNMENT_STATUSES or assignment.status == WorkAssignmentStatus.running:
        return None, None
    decision: CoordinatorDecision = evaluate_assignment_readiness(db, assignment=assignment)
    if decision.outcome == "ASSIGNABLE":
        return None, None
    return decision.outcome, decision.reason


def assignment_runtime_view(
    db: Session, assignment: AgentWorkAssignment, *, agent: CoordinationAgent | None = None
) -> AssignmentRuntimeView:
    agent = agent if agent is not None else db.get(CoordinationAgent, assignment.agent_id)
    block_outcome, block_reason = _block_reason_for(db, assignment)
    active_lease_id = db.execute(
        select(AgentScopeLease.id).where(
            AgentScopeLease.assignment_id == assignment.id, AgentScopeLease.status == AgentScopeLeaseStatus.active
        )
    ).scalar_one_or_none()
    return AssignmentRuntimeView(
        assignment_id=assignment.id,
        agent_id=assignment.agent_id,
        agent_key=agent.agent_key if agent is not None else "",
        role=assignment.role.value,
        runtime_status=_ASSIGNMENT_RUNTIME_STATUS[assignment.status],
        canonical_status=assignment.status.value,
        read_write_mode=assignment.read_write_mode.value,
        repository_identity=assignment.repository_identity,
        branch=assignment.branch,
        worktree_path=assignment.worktree_path,
        base_sha=assignment.base_sha,
        head_sha=assignment.head_sha,
        allowed_paths=tuple(assignment.allowed_paths),
        goal_id=assignment.goal_id,
        task_id=assignment.task_id,
        parallel_exploration_group_id=assignment.parallel_exploration_group_id,
        upstream_pr_ref=assignment.upstream_pr_ref,
        block_reason=block_reason,
        block_outcome=block_outcome,
        active_lease_id=active_lease_id,
        started_at=assignment.started_at,
        completed_at=assignment.completed_at,
    )


def _current_assignments_for_agent(db: Session, *, agent_id: uuid.UUID, owner_id: uuid.UUID) -> list[AgentWorkAssignment]:
    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    return list(
        db.execute(
            select(AgentWorkAssignment)
            .where(
                AgentWorkAssignment.agent_id == agent_id,
                AgentWorkAssignment.owner_id == owner_id,
                AgentWorkAssignment.status.notin_(terminal),
            )
            .order_by(AgentWorkAssignment.created_at)
        ).scalars()
    )


def agent_runtime_snapshot(db: Session, agent: CoordinationAgent, *, owner_id: uuid.UUID) -> AgentRuntimeView:
    """One agent's complete current-state snapshot. `owner_id` is required and filters
    `agent_work_assignments` explicitly -- defense in depth alongside RLS, matching this
    codebase's established doctrine (see e.g.
    app.agent_coordination.service.create_work_assignment's own identical reasoning), never
    relying on the session's RLS context alone even though `coordination_agents` itself is
    deliberately founder-wide and unfiltered (see that model's own docstring)."""

    available, reason = agent_availability(db, agent_id=agent.id)
    assignments = _current_assignments_for_agent(db, agent_id=agent.id, owner_id=owner_id)
    views = tuple(assignment_runtime_view(db, a, agent=agent) for a in assignments)

    if agent.status != CoordinationAgentStatus.active:
        runtime_status = RuntimeStatus.OFFLINE
    else:
        runtime_status = RuntimeStatus.IDLE
        for candidate in _AGENT_STATUS_PRIORITY:
            if any(v.runtime_status == candidate for v in views):
                runtime_status = candidate
                break

    active_lease_ids = [v.active_lease_id for v in views if v.active_lease_id is not None]
    heartbeat_at = None
    if active_lease_ids:
        heartbeat_at = db.execute(
            select(AgentScopeLease.last_heartbeat_at)
            .where(AgentScopeLease.id.in_(active_lease_ids))
            .order_by(AgentScopeLease.last_heartbeat_at.desc().nullslast())
            .limit(1)
        ).scalar_one_or_none()

    return AgentRuntimeView(
        agent_id=agent.id,
        agent_key=agent.agent_key,
        display_name=agent.display_name,
        adapter_kind=agent.adapter_kind.value,
        model_hint=agent.model_hint,
        capabilities=tuple(agent.capabilities),
        concurrency_limit=agent.concurrency_limit,
        registry_status=agent.status.value,
        runtime_status=runtime_status,
        availability_reason=reason,
        heartbeat_at=heartbeat_at,
        current_assignments=views,
    )


def all_agents_runtime_snapshot(db: Session, *, owner_id: uuid.UUID, agent_ids: list[uuid.UUID] | None = None) -> list[AgentRuntimeView]:
    """Every registered agent's snapshot in one call -- "Life sees all of them" (Claude Code,
    Cursor Agent, Codex, ...). `coordination_agents` is founder-wide (never owner-scoped, see
    that model's own docstring), so this iterates the FULL registry regardless of `owner_id` --
    only each agent's ASSIGNMENTS are owner-scoped, filtered explicitly per agent inside
    `agent_runtime_snapshot()` above."""

    query = select(CoordinationAgent).order_by(CoordinationAgent.agent_key)
    if agent_ids is not None:
        query = query.where(CoordinationAgent.id.in_(agent_ids))
    agents = list(db.execute(query).scalars())
    return [agent_runtime_snapshot(db, agent, owner_id=owner_id) for agent in agents]


def work_registry_snapshot(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID | None = None) -> list[AssignmentRuntimeView]:
    """Every current (non-terminal) assignment for this owner, across every agent -- "WHO is
    doing WHAT, WHERE, with WHAT authority, for WHICH goal/task, with WHICH dependency/wait
    reason" without having to query per-agent first."""

    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    query = select(AgentWorkAssignment).where(
        AgentWorkAssignment.owner_id == owner_id, AgentWorkAssignment.status.notin_(terminal)
    )
    if goal_id is not None:
        query = query.where(AgentWorkAssignment.goal_id == goal_id)
    assignments = list(db.execute(query.order_by(AgentWorkAssignment.created_at)).scalars())

    agent_ids = {a.agent_id for a in assignments}
    agents_by_id: dict[uuid.UUID, CoordinationAgent] = {}
    if agent_ids:
        agents_by_id = {a.id: a for a in db.execute(select(CoordinationAgent).where(CoordinationAgent.id.in_(agent_ids))).scalars()}

    return [assignment_runtime_view(db, a, agent=agents_by_id.get(a.agent_id)) for a in assignments]
