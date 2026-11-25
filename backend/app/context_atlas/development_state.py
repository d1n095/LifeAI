"""Durable development-state recovery over existing coordination and project memory (E1/E3).

This module adds no task engine, no assignment table and no second checkpoint store. It reads
the evidence LifeAI already persists:

- `agent_work_assignments` + `agent_work_assignment_events` for objective and history,
- `agent_scope_leases` for current write authority,
- `agent_dispatch_executions` for the last observed process/session state,
- `mainai_goals` / `mainai_tasks` for the authorized objective and approval flags,
- `project_checkpoints` / `project_notes` for the human-readable resumption state.

A persisted row is evidence that something was recorded, not proof that an agent is alive.
`DevelopmentState.observed` is therefore only true when a dispatch row is still in a live
state with a recent heartbeat. A `running` assignment without a live lease is reported as
stale, not as running.

Recovery prefers persisted evidence over any caller recollection. It never dispatches,
never resumes an adapter and never grants execution authority.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.service import is_lease_stale
from app.models.agent_coordination import (
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
    AgentDispatchExecution,
    AgentScopeLease,
    AgentScopeLeaseStatus,
    AgentWorkAssignment,
    AgentWorkAssignmentDependency,
    AgentWorkAssignmentEvent,
    CoordinationAgent,
    ExecutionAdapterState,
    WorkAssignmentStatus,
)
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.project_memory import NoteKind, NoteStatus
from app.project_memory import get_latest_checkpoint, is_checkpoint_stale, list_notes

# A dispatch row is only "observed" while its adapter state is live and its heartbeat is
# recent. This is deliberately conservative: a process that stopped reporting is not alive
# merely because its last row still says `running`.
OBSERVATION_WINDOW = timedelta(minutes=15)

_ACTIVE_DISPATCH_STATES = frozenset(
    {ExecutionAdapterState.starting, ExecutionAdapterState.running}
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _enum(value: object) -> str:
    return str(getattr(value, "value", value))


def _latest_event(
    db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID
) -> AgentWorkAssignmentEvent | None:
    return db.execute(
        select(AgentWorkAssignmentEvent)
        .where(
            AgentWorkAssignmentEvent.owner_id == owner_id,
            AgentWorkAssignmentEvent.assignment_id == assignment_id,
        )
        .order_by(AgentWorkAssignmentEvent.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_lease(
    db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID
) -> AgentScopeLease | None:
    return db.execute(
        select(AgentScopeLease)
        .where(
            AgentScopeLease.owner_id == owner_id,
            AgentScopeLease.assignment_id == assignment_id,
            AgentScopeLease.status == AgentScopeLeaseStatus.active,
        )
        .order_by(AgentScopeLease.lease_generation.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_dispatch(
    db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID
) -> AgentDispatchExecution | None:
    return db.execute(
        select(AgentDispatchExecution)
        .where(
            AgentDispatchExecution.owner_id == owner_id,
            AgentDispatchExecution.assignment_id == assignment_id,
        )
        .order_by(AgentDispatchExecution.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _is_observed(dispatch: AgentDispatchExecution | None, *, now: datetime) -> bool:
    if dispatch is None or dispatch.adapter_state not in _ACTIVE_DISPATCH_STATES:
        return False
    heartbeat = dispatch.last_heartbeat_at or dispatch.last_output_at or dispatch.started_at
    return heartbeat is not None and now - heartbeat <= OBSERVATION_WINDOW


@dataclass(frozen=True)
class Milestone:
    event_type: str
    at: str
    detail: str

    def to_dict(self) -> dict:
        return {"event_type": self.event_type, "at": self.at, "detail": self.detail}


@dataclass(frozen=True)
class DevelopmentState:
    assignment_id: str
    agent_key: str | None
    role: str
    assignment_status: str
    objective: str
    goal_status: str | None
    task_status: str | None
    branch: str | None
    worktree_path: str | None
    base_sha: str | None
    head_sha: str | None
    upstream_pr_ref: str | None
    allowed_paths: tuple[str, ...]
    lease_live: bool
    lease_generation: int | None
    lease_expires_at: str | None
    adapter_state: str | None
    last_heartbeat_at: str | None
    observed: bool
    latest_event_at: str | None
    latest_event_type: str | None
    completed_milestones: tuple[Milestone, ...]
    dependency_status: tuple[str, ...]
    blockers: tuple[str, ...]
    decisions: tuple[str, ...]
    checkpoint_summary: str | None
    checkpoint_sha: str | None
    checkpoint_created_at: str | None
    checkpoint_stale: bool | None
    next_authorized_action: str
    approval_gates: tuple[str, ...]
    evidence: tuple[str, ...]
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "agent_key": self.agent_key,
            "role": self.role,
            "assignment_status": self.assignment_status,
            "objective": self.objective,
            "goal_status": self.goal_status,
            "task_status": self.task_status,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "upstream_pr_ref": self.upstream_pr_ref,
            "allowed_paths": list(self.allowed_paths),
            "lease_live": self.lease_live,
            "lease_generation": self.lease_generation,
            "lease_expires_at": self.lease_expires_at,
            "adapter_state": self.adapter_state,
            "last_heartbeat_at": self.last_heartbeat_at,
            "observed": self.observed,
            "latest_event_at": self.latest_event_at,
            "latest_event_type": self.latest_event_type,
            "completed_milestones": [m.to_dict() for m in self.completed_milestones],
            "dependency_status": list(self.dependency_status),
            "blockers": list(self.blockers),
            "decisions": list(self.decisions),
            "checkpoint_summary": self.checkpoint_summary,
            "checkpoint_sha": self.checkpoint_sha,
            "checkpoint_created_at": self.checkpoint_created_at,
            "checkpoint_stale": self.checkpoint_stale,
            "next_authorized_action": self.next_authorized_action,
            "approval_gates": list(self.approval_gates),
            "evidence": list(self.evidence),
            "execution_authorized": False,
        }


def _dependency_status(
    db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID
) -> tuple[str, ...]:
    dependency_ids = list(
        db.execute(
            select(AgentWorkAssignmentDependency.depends_on_assignment_id).where(
                AgentWorkAssignmentDependency.owner_id == owner_id,
                AgentWorkAssignmentDependency.assignment_id == assignment_id,
            )
        ).scalars()
    )
    if not dependency_ids:
        return ()
    rows = {
        row.id: row
        for row in db.execute(
            select(AgentWorkAssignment).where(
                AgentWorkAssignment.owner_id == owner_id,
                AgentWorkAssignment.id.in_(dependency_ids),
            )
        ).scalars()
    }
    return tuple(
        f"{dep_id}: {_enum(rows[dep_id].status) if dep_id in rows else 'missing'}"
        for dep_id in dependency_ids
    )


def _next_action(
    assignment: AgentWorkAssignment, *, lease_live: bool, observed: bool
) -> str:
    status = _enum(assignment.status)
    if status in {s.value for s in TERMINAL_WORK_ASSIGNMENT_STATUSES}:
        return "none; the assignment is terminal and must not be resumed"
    if status == WorkAssignmentStatus.blocked.value:
        return "resolve the recorded blocker and add new evidence; do not dispatch again"
    if lease_live:
        return "resume from the last durable event; do not start a second agent"
    if observed:
        return "observe the existing process; acquire a lease before any write"
    if (
        assignment.read_write_mode.value == "read_write"
        and status in {WorkAssignmentStatus.ready.value, WorkAssignmentStatus.running.value}
    ):
        return "acquire a scope lease through the existing coordinator before any write"
    if status == WorkAssignmentStatus.ready.value:
        return "claim or dispatch through the existing coordinator; no automatic start"
    return "inspect the persisted events; do not infer a state that was not recorded"


def load_development_state(
    db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID
) -> DevelopmentState | None:
    """Load one assignment's durable state. Read-only and owner-scoped."""

    assignment = db.execute(
        select(AgentWorkAssignment).where(
            AgentWorkAssignment.id == assignment_id,
            AgentWorkAssignment.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        return None

    now = datetime.utcnow()
    goal = db.get(MainAIGoal, assignment.goal_id)
    task = db.get(MainAITask, assignment.task_id) if assignment.task_id else None
    agent = db.get(CoordinationAgent, assignment.agent_id)
    event = _latest_event(db, owner_id=owner_id, assignment_id=assignment.id)
    lease = _latest_lease(db, owner_id=owner_id, assignment_id=assignment.id)
    lease_live = lease is not None and not is_lease_stale(lease, now=now)
    dispatch = _latest_dispatch(db, owner_id=owner_id, assignment_id=assignment.id)
    observed = _is_observed(dispatch, now=now)

    events = list(
        db.execute(
            select(AgentWorkAssignmentEvent)
            .where(
                AgentWorkAssignmentEvent.owner_id == owner_id,
                AgentWorkAssignmentEvent.assignment_id == assignment.id,
            )
            .order_by(AgentWorkAssignmentEvent.created_at.asc())
        ).scalars()
    )
    milestones = tuple(
        Milestone(
            event_type=_enum(item.event_type),
            at=_iso(item.created_at) or "",
            detail=str(item.detail.get("to") or item.detail.get("summary") or ""),
        )
        for item in events
        if _enum(item.event_type) == "status_changed"
        or item.detail.get("to") in {"completed", "verified"}
    )[-10:]

    open_notes = list_notes(db, status=NoteStatus.open)
    blockers = tuple(n.content for n in open_notes if n.kind is NoteKind.blocker)
    decisions = tuple(n.content for n in open_notes if n.kind is NoteKind.decision)

    checkpoint = get_latest_checkpoint(db)
    checkpoint_stale: bool | None = None
    if checkpoint is not None:
        checkpoint_stale, _ = is_checkpoint_stale(db, checkpoint)

    approval_gates: list[str] = []
    if assignment.approval_required:
        approval_gates.append("assignment.approval_required")
    if task is not None and task.approval_required:
        approval_gates.append("task.approval_required")
    if goal is not None:
        approval_gates.append(f"goal.approval_policy={goal.approval_policy}")

    evidence = [
        f"assignment {assignment.id}: status={_enum(assignment.status)}",
        f"goal {assignment.goal_id}: status={_enum(goal.status) if goal else 'missing'}",
        f"lease: {'live generation ' + str(lease.lease_generation) if lease_live and lease else 'none live'}",
        f"dispatch: {_enum(dispatch.adapter_state) if dispatch else 'none'}",
        f"latest event: {_enum(event.event_type) if event else 'none'}",
        f"checkpoint: {checkpoint.id if checkpoint else 'none'}",
    ]

    return DevelopmentState(
        assignment_id=str(assignment.id),
        agent_key=agent.agent_key if agent is not None else None,
        role=_enum(assignment.role),
        assignment_status=_enum(assignment.status),
        objective=goal.original_instruction if goal is not None else assignment.canonical_work_key,
        goal_status=_enum(goal.status) if goal is not None else None,
        task_status=_enum(task.status) if task is not None else None,
        branch=assignment.branch,
        worktree_path=assignment.worktree_path,
        base_sha=assignment.base_sha,
        head_sha=assignment.head_sha,
        upstream_pr_ref=assignment.upstream_pr_ref,
        allowed_paths=tuple(assignment.allowed_paths or ()),
        lease_live=lease_live,
        lease_generation=lease.lease_generation if lease_live and lease is not None else None,
        lease_expires_at=_iso(lease.expires_at) if lease is not None else None,
        adapter_state=_enum(dispatch.adapter_state) if dispatch is not None else None,
        last_heartbeat_at=_iso(dispatch.last_heartbeat_at) if dispatch is not None else None,
        observed=observed,
        latest_event_at=_iso(event.created_at) if event is not None else None,
        latest_event_type=_enum(event.event_type) if event is not None else None,
        completed_milestones=milestones,
        dependency_status=_dependency_status(
            db, owner_id=owner_id, assignment_id=assignment.id
        ),
        blockers=blockers,
        decisions=decisions,
        checkpoint_summary=checkpoint.summary if checkpoint is not None else None,
        checkpoint_sha=checkpoint.git_commit_sha if checkpoint is not None else None,
        checkpoint_created_at=_iso(checkpoint.created_at) if checkpoint is not None else None,
        checkpoint_stale=checkpoint_stale,
        next_authorized_action=_next_action(
            assignment, lease_live=lease_live, observed=observed
        ),
        approval_gates=tuple(approval_gates),
        evidence=tuple(evidence),
    )


@dataclass(frozen=True)
class RecoveryCandidate:
    assignment_id: str
    agent_key: str | None
    branch: str | None
    status: str
    latest_activity_at: str | None
    lease_live: bool
    observed: bool
    stale: bool
    head_sha: str | None
    base_sha: str | None
    upstream_pr_ref: str | None
    reason: str

    def to_dict(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "agent_key": self.agent_key,
            "branch": self.branch,
            "status": self.status,
            "latest_activity_at": self.latest_activity_at,
            "lease_live": self.lease_live,
            "observed": self.observed,
            "stale": self.stale,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "upstream_pr_ref": self.upstream_pr_ref,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RecoveryReport:
    status: str  # recovered | stale | ambiguous | none
    selected_assignment_id: str | None
    candidates: tuple[RecoveryCandidate, ...]
    reason: str
    next_action: str
    requires_founder: bool
    github_observed: bool = False
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "selected_assignment_id": self.selected_assignment_id,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "reason": self.reason,
            "next_action": self.next_action,
            "requires_founder": self.requires_founder,
            "github_observed": False,
            "execution_authorized": False,
        }


def _candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: AgentWorkAssignment,
    now: datetime,
) -> RecoveryCandidate:
    event = _latest_event(db, owner_id=owner_id, assignment_id=assignment.id)
    lease = _latest_lease(db, owner_id=owner_id, assignment_id=assignment.id)
    lease_live = lease is not None and not is_lease_stale(lease, now=now)
    dispatch = _latest_dispatch(db, owner_id=owner_id, assignment_id=assignment.id)
    observed = _is_observed(dispatch, now=now)

    timestamps = [
        assignment.updated_at,
        assignment.started_at,
        event.created_at if event is not None else None,
        lease.last_heartbeat_at if lease is not None else None,
        dispatch.last_heartbeat_at if dispatch is not None else None,
        dispatch.last_output_at if dispatch is not None else None,
    ]
    activity = max((value for value in timestamps if value is not None), default=None)
    status = _enum(assignment.status)
    stale = status == WorkAssignmentStatus.running.value and not lease_live

    reasons: list[str] = []
    if lease_live:
        reasons.append("a live scope lease is recorded")
    elif status == WorkAssignmentStatus.running.value:
        reasons.append("status is running but no live lease is recorded")
    if observed:
        reasons.append("a live dispatch row has a recent heartbeat")
    else:
        reasons.append("no live dispatch observation")
    if assignment.head_sha and assignment.base_sha and assignment.head_sha != assignment.base_sha:
        reasons.append("local commits are recorded; push state is unknown")
    if assignment.upstream_pr_ref:
        reasons.append(f"persisted upstream ref: {assignment.upstream_pr_ref}")
    else:
        reasons.append("no upstream PR ref is persisted; GitHub was not contacted")

    agent = db.get(CoordinationAgent, assignment.agent_id)
    return RecoveryCandidate(
        assignment_id=str(assignment.id),
        agent_key=agent.agent_key if agent is not None else None,
        branch=assignment.branch,
        status=status,
        latest_activity_at=_iso(activity),
        lease_live=lease_live,
        observed=observed,
        stale=stale,
        head_sha=assignment.head_sha,
        base_sha=assignment.base_sha,
        upstream_pr_ref=assignment.upstream_pr_ref,
        reason="; ".join(reasons),
    )


def recover_development_state(
    db: Session,
    *,
    owner_id: uuid.UUID,
    repository_identity: str | None = None,
    branch: str | None = None,
) -> RecoveryReport:
    """Choose the persisted assignment a new session should recover, or say why not.

    Selection is deterministic and evidence-first: a live lease outranks an observed
    process, which outranks the latest recorded activity. Two live leases (or an exact tie)
    are ambiguous and require a founder decision. A `running` assignment with no live lease
    is returned as `stale`, never as `recovered`.

    No network call is made. `github_observed` is always false; a persisted
    `upstream_pr_ref` is reported as recorded evidence only.
    """

    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    query = select(AgentWorkAssignment).where(
        AgentWorkAssignment.owner_id == owner_id,
        AgentWorkAssignment.status.notin_(terminal),
    )
    if repository_identity is not None:
        query = query.where(
            AgentWorkAssignment.repository_identity == repository_identity
        )
    if branch is not None:
        query = query.where(AgentWorkAssignment.branch == branch)
    assignments = list(db.execute(query).scalars())

    if not assignments:
        return RecoveryReport(
            status="none",
            selected_assignment_id=None,
            candidates=(),
            reason="no non-terminal assignment matches the requested owner/scope",
            next_action="do not infer a task; ask for the objective or create a new authorized assignment",
            requires_founder=False,
        )

    now = datetime.utcnow()
    candidates = [
        _candidate(db, owner_id=owner_id, assignment=assignment, now=now)
        for assignment in assignments
    ]
    candidates.sort(
        key=lambda c: (
            1 if c.lease_live else 0,
            1 if c.observed else 0,
            c.latest_activity_at or "",
        ),
        reverse=True,
    )

    live = [candidate for candidate in candidates if candidate.lease_live]
    if len(live) > 1:
        return RecoveryReport(
            status="ambiguous",
            selected_assignment_id=None,
            candidates=tuple(candidates),
            reason="more than one live scope lease exists; selecting one would invent ownership",
            next_action="ask the founder which assignment is current; do not dispatch or resume",
            requires_founder=True,
        )

    if len(candidates) > 1:
        first, second = candidates[0], candidates[1]
        if (
            first.lease_live == second.lease_live
            and first.observed == second.observed
            and first.latest_activity_at == second.latest_activity_at
        ):
            return RecoveryReport(
                status="ambiguous",
                selected_assignment_id=None,
                candidates=tuple(candidates),
                reason="two assignments have identical persisted evidence; recency cannot decide",
                next_action="ask the founder which assignment is current; do not guess",
                requires_founder=True,
            )

    selected = candidates[0]
    if selected.stale:
        return RecoveryReport(
            status="stale",
            selected_assignment_id=selected.assignment_id,
            candidates=tuple(candidates),
            reason="the newest assignment says running, but no live lease or recent process heartbeat proves it",
            next_action="verify the process and lease before resuming; do not assume it is running",
            requires_founder=False,
        )

    return RecoveryReport(
        status="recovered",
        selected_assignment_id=selected.assignment_id,
        candidates=tuple(candidates),
        reason="selected from persisted assignment, lease, event and dispatch evidence",
        next_action="load the selected assignment's durable state; resume only through the existing coordinator",
        requires_founder=False,
    )
