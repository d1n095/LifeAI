"""Multi-Agent Work Coordination -- deterministic conflict prevention and assignment
bookkeeping for multiple simultaneous external coding agents (Claude Code, Cursor Agent,
Codex, future CLI/API providers) working against the SAME repository.

This module never mints a second task/job queue (assignments always reference an existing,
already-authorized `MainAIGoal`/`MainAITask` -- see app/models/mainai_execution.py), never
bypasses `app.mainai_execution.approval.require_task_approval()` (an assignment's own
`approval_required` flag is descriptive coordination metadata, not a second enforcement gate),
and never invokes a real external agent (see app.agent_coordination.adapters' own docstring).
Capability/performance evidence is recorded through the existing
`app.intelligence_governance` primitives, referenced by id, never duplicated.

Conflict detection is ENTIRELY deterministic path-prefix intersection (`paths_conflict`/
`paths_conflict_any` below) -- never natural-language reasoning about whether two scopes
"probably" overlap. See docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md for the full architecture.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intelligence_governance import record_evidence, record_execution
from app.models.agent_coordination import (
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
    AgentAdapterKind,
    AgentScopeLease,
    AgentScopeLeaseStatus,
    AgentWorkAssignment,
    AgentWorkAssignmentDependency,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    CoordinationAgent,
    CoordinationAgentStatus,
    ParallelExplorationGroup,
    WorkAssignmentReadWriteMode,
    WorkAssignmentRole,
    WorkAssignmentStatus,
)
from app.models.mainai_execution import MainAIGoal


class CoordinationError(ValueError):
    """Base for genuine misuse -- an assignment/agent/lease that does not exist, or a call
    that violates an invariant a well-behaved caller should never hit (as opposed to an
    ordinary, expected conflict outcome -- see CoordinatorDecision/LeaseAcquisitionResult for
    those)."""


class AgentNotFoundError(CoordinationError):
    pass


class AssignmentNotFoundError(CoordinationError):
    pass


class DuplicateWorkError(CoordinationError):
    """Raised by create_work_assignment() when an equivalent, non-terminal assignment for the
    SAME canonical work already exists and the caller did not explicitly opt into
    PARALLEL_EXPLORATION via `parallel_exploration_group_id` -- rule: accidental duplication is
    rejected, intentional competition is not."""


class ScopeExpansionError(CoordinationError):
    """Raised when a lease renewal/replay requests a path scope that is not a subset of the
    scope the assignment already held -- "a worker may never silently expand its own path
    scope.\""""


class LeaseFencingError(CoordinationError):
    """Raised when a caller presents a stale `lease_generation` or the wrong `agent_id` for a
    mutating lease operation (renew/release/takeover-race) -- the exact
    `mainai_jobs.lease_generation` fencing convention, applied here."""


class LeaseStillLiveError(CoordinationError):
    """Raised by take_over_lease() when the target lease is not actually stale -- a live
    worker's lease can only be released by that worker, never silently overridden."""


class InvalidTransitionError(CoordinationError):
    """Raised by transition_status() for a status transition not in ALLOWED_TRANSITIONS."""


# ============================================================================ deterministic
# path-prefix conflict detection -- the ONLY thing collision enforcement is allowed to reason
# over; see module docstring.

def _segments(path_prefix: str) -> tuple[str, ...]:
    """Normalizes a path-prefix string ("backend/app/finance/**", an exact file path, or a
    bare directory) to its segment tuple -- strips a trailing glob (`/**`, `/*`, `*`) and any
    trailing slash, then splits on "/". Two prefixes "conflict" (see paths_conflict below) when
    one's segment tuple is a prefix of the other's -- i.e. one path is an ancestor of, or
    identical to, the other; this is directory-boundary-aware, NOT a raw string prefix
    (`backend/app/dev` must never be treated as overlapping `backend/app/development_x/...`
    merely because the raw strings share a prefix)."""

    normalized = path_prefix.strip()
    for suffix in ("/**", "/*", "**", "*"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    normalized = normalized.rstrip("/")
    return tuple(part for part in normalized.split("/") if part)


def paths_conflict(a: str, b: str) -> bool:
    """True iff path-prefix `a` and path-prefix `b` overlap: one is an ancestor-or-equal of
    the other. Pure, deterministic, no filesystem access."""

    sa, sb = _segments(a), _segments(b)
    n = min(len(sa), len(sb))
    return sa[:n] == sb[:n]


def paths_conflict_any(paths_a: list[str] | tuple[str, ...], paths_b: list[str] | tuple[str, ...]) -> bool:
    """True iff ANY prefix in `paths_a` conflicts with ANY prefix in `paths_b`. An empty scope
    on either side never conflicts (repo-wide READ_ONLY is handled by the caller skipping this
    check entirely for read-only scopes -- see acquire_lease -- not by treating an empty list
    as "no scope")."""

    if not paths_a or not paths_b:
        return False
    return any(paths_conflict(a, b) for a in paths_a for b in paths_b)


# ============================================================================ agent registry

def register_agent(
    db: Session,
    *,
    agent_key: str,
    display_name: str,
    adapter_kind: str,
    capabilities: list[str] | tuple[str, ...] = (),
    execution_mode: str = "unknown",
    supports_read: bool = True,
    supports_write: bool = False,
    model_hint: str | None = None,
    cost_class: str = "unknown",
    concurrency_limit: int = 1,
    status: str = CoordinationAgentStatus.active.value,
) -> CoordinationAgent:
    """Idempotent upsert by `agent_key` -- registering the same agent again updates its
    profile in place rather than creating a duplicate row, so a restart/reload of whatever
    process owns the registry converges on the same identity instead of accumulating stale
    duplicates."""

    existing = db.execute(select(CoordinationAgent).where(CoordinationAgent.agent_key == agent_key)).scalar_one_or_none()
    values = {
        "display_name": display_name,
        # Coerced to the real enum member (not left as a caller-supplied string) so every
        # in-memory read of this attribute within the SAME transaction -- before any DB
        # round-trip -- sees a proper enum instance, never a bare string; see
        # app.development_supervisor.service.discover_candidates()'s own
        # `getattr(task.risk_level, "value", task.risk_level)` defensive workaround for what
        # happens when a caller relies on that round-trip instead.
        "adapter_kind": AgentAdapterKind(adapter_kind),
        "capabilities": list(capabilities),
        "execution_mode": execution_mode,
        "supports_read": supports_read,
        "supports_write": supports_write,
        "model_hint": model_hint,
        "cost_class": cost_class,
        "concurrency_limit": concurrency_limit,
        "status": CoordinationAgentStatus(status),
    }
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        existing.updated_at = datetime.utcnow()
        db.flush()
        return existing
    row = CoordinationAgent(agent_key=agent_key, **values)
    db.add(row)
    db.flush()
    return row


def get_agent(db: Session, agent_id: uuid.UUID) -> CoordinationAgent:
    agent = db.get(CoordinationAgent, agent_id)
    if agent is None:
        raise AgentNotFoundError(f"CoordinationAgent {agent_id} does not exist")
    return agent


def get_agent_by_key(db: Session, agent_key: str) -> CoordinationAgent:
    agent = db.execute(select(CoordinationAgent).where(CoordinationAgent.agent_key == agent_key)).scalar_one_or_none()
    if agent is None:
        raise AgentNotFoundError(f"CoordinationAgent '{agent_key}' does not exist")
    return agent


# Statuses that consume one of an agent's `concurrency_limit` slots -- deliberately EXCLUDES
# `planned`/`waiting_dependency`/`ready` (queued -- not yet actually occupying the agent's own
# attention, the same "queued vs claimed" distinction CLAIMABLE_MAINAI_JOB_STATUSES draws
# elsewhere in this codebase) in addition to the terminal statuses. A duplicate-work check
# (is_duplicate_canonical_work) uses the wider "any non-terminal status" set instead -- a
# PLANNED or READY duplicate is still a real duplicate to reject, even though neither has
# started consuming a concurrency slot yet.
CONCURRENCY_CONSUMING_STATUSES = frozenset(
    {
        WorkAssignmentStatus.running,
        WorkAssignmentStatus.waiting_agent,
        WorkAssignmentStatus.waiting_review,
        WorkAssignmentStatus.blocked,
        WorkAssignmentStatus.ready_for_review,
        WorkAssignmentStatus.reviewing,
        WorkAssignmentStatus.changes_requested,
        WorkAssignmentStatus.verified,
    }
)


def count_active_assignments(db: Session, *, agent_id: uuid.UUID) -> int:
    """Derived, never stored -- a stored counter column would be a second source of truth that
    could drift from the assignments table it summarizes (the same reasoning this codebase
    applies everywhere else; see e.g. MainAIJob's own lack of a redundant queue-depth
    column)."""

    consuming = tuple(status.value for status in CONCURRENCY_CONSUMING_STATUSES)
    rows = db.execute(
        select(AgentWorkAssignment.id).where(
            AgentWorkAssignment.agent_id == agent_id,
            AgentWorkAssignment.status.in_(consuming),
        )
    ).all()
    return len(rows)


def agent_availability(db: Session, *, agent_id: uuid.UUID) -> tuple[bool, str]:
    agent = get_agent(db, agent_id)
    if agent.status != CoordinationAgentStatus.active:
        return False, f"agent status is '{agent.status.value}', not 'active'"
    active = count_active_assignments(db, agent_id=agent_id)
    if active >= agent.concurrency_limit:
        return False, f"agent is at its concurrency limit ({active}/{agent.concurrency_limit})"
    return True, "agent is available"


# ============================================================================ parallel
# exploration groups

def create_parallel_exploration_group(
    db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID, canonical_problem_ref: str, created_by: str
) -> ParallelExplorationGroup:
    row = ParallelExplorationGroup(
        owner_id=owner_id, goal_id=goal_id, canonical_problem_ref=canonical_problem_ref, created_by=created_by
    )
    db.add(row)
    db.flush()
    return row


# ============================================================================ work assignments

def _canonical_work_key(
    *, goal_id: uuid.UUID, task_id: uuid.UUID | None, role: str, repository_identity: str = "", allowed_paths: list[str] | tuple[str, ...] = ()
) -> str:
    """When `task_id` is set, it alone already identifies one discrete unit of work in the
    goal's task graph -- sufficient on its own. When it is NOT set (an assignment ahead of a
    formal MainAITask, e.g. early exploratory work), `task_id` collapses to nothing and would
    make EVERY task_id-less assignment under the same goal+role collide with every OTHER one,
    regardless of what part of the repository each actually targets -- so the key falls back
    to (repository_identity, sorted allowed_paths) instead, the next-most-specific thing that
    actually distinguishes "the same canonical work" from "two unrelated pieces of work that
    both happen to lack a task yet.\""""

    if task_id is not None:
        return f"{goal_id}:{task_id}:{role}"
    scope = ",".join(sorted(allowed_paths))
    return f"{goal_id}:no-task:{repository_identity}:{role}:{scope}"


def _record_event(
    db: Session, assignment: AgentWorkAssignment, *, event_type: AgentWorkAssignmentEventType, detail: dict | None = None
) -> AgentWorkAssignmentEvent:
    event = AgentWorkAssignmentEvent(
        assignment_id=assignment.id, owner_id=assignment.owner_id, event_type=event_type, detail=detail or {}
    )
    db.add(event)
    db.flush()
    return event


def is_duplicate_canonical_work(
    db: Session, *, owner_id: uuid.UUID, canonical_work_key: str, parallel_exploration_group_id: uuid.UUID | None
) -> AgentWorkAssignment | None:
    """An explicit `parallel_exploration_group_id` marks this as INTENTIONAL competition -- the
    duplicate check is skipped entirely in that case (any number of assignments may share a
    canonical_work_key once each is placed in a parallel-exploration group). With no group,
    ANY existing non-terminal assignment for the same canonical work is a duplicate,
    regardless of what group (if any) THAT one happens to be in."""

    if parallel_exploration_group_id is not None:
        return None
    terminal = tuple(status.value for status in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    return db.execute(
        select(AgentWorkAssignment).where(
            AgentWorkAssignment.owner_id == owner_id,
            AgentWorkAssignment.canonical_work_key == canonical_work_key,
            AgentWorkAssignment.status.notin_(terminal),
        )
    ).scalars().first()


_REVIEW_ONLY_ROLES = frozenset({WorkAssignmentRole.reviewer, WorkAssignmentRole.researcher})

# Instance-satisfying dependency-release rule: a dependent (e.g. a REVIEWER) is releasable
# once its dependency reaches one of these -- "dependency completion releases reviewer" means
# reaching an actually-reviewable state, not necessarily fully COMPLETED.
DEPENDENCY_RELEASE_STATUSES = frozenset(
    {
        WorkAssignmentStatus.ready_for_review.value,
        WorkAssignmentStatus.verified.value,
        WorkAssignmentStatus.completed.value,
    }
)


def create_work_assignment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    task_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    role: str,
    read_write_mode: str,
    repository_identity: str,
    allowed_paths: list[str] | tuple[str, ...],
    requested_by: str,
    risk_level: str = "low",
    approval_required: bool = False,
    parallel_exploration_group_id: uuid.UUID | None = None,
    depends_on_assignment_ids: tuple[uuid.UUID, ...] = (),
    provenance: dict[str, Any] | None = None,
) -> AgentWorkAssignment:
    """Creates one durable agent job. Duplicate-work and reviewer/read-only invariants are
    enforced here (in addition to the DB CHECK constraints -- defense in depth, never relying
    on a single layer alone, matching this codebase's established doctrine)."""

    get_agent(db, agent_id)  # AgentNotFoundError if missing
    # Coerced to real enum members immediately -- see register_agent()'s own comment on why
    # this matters for every in-memory `.value`/equality read below, this transaction, before
    # any DB round-trip.
    role = WorkAssignmentRole(role)
    read_write_mode = WorkAssignmentReadWriteMode(read_write_mode)
    if role in _REVIEW_ONLY_ROLES and read_write_mode != WorkAssignmentReadWriteMode.read_only:
        raise CoordinationError(f"role '{role.value}' must be assigned read_write_mode='read_only'")

    canonical_work_key = _canonical_work_key(
        goal_id=goal_id, task_id=task_id, role=role.value, repository_identity=repository_identity, allowed_paths=allowed_paths
    )
    duplicate = is_duplicate_canonical_work(
        db, owner_id=owner_id, canonical_work_key=canonical_work_key, parallel_exploration_group_id=parallel_exploration_group_id
    )
    if duplicate is not None:
        raise DuplicateWorkError(
            f"an active assignment ({duplicate.id}) already covers canonical work '{canonical_work_key}' -- "
            "pass parallel_exploration_group_id to opt into intentional competition instead."
        )

    row = AgentWorkAssignment(
        owner_id=owner_id,
        goal_id=goal_id,
        task_id=task_id,
        agent_id=agent_id,
        role=role,
        read_write_mode=read_write_mode,
        repository_identity=repository_identity,
        allowed_paths=list(allowed_paths),
        status=WorkAssignmentStatus.planned,
        risk_level=risk_level,
        approval_required=approval_required,
        parallel_exploration_group_id=parallel_exploration_group_id,
        canonical_work_key=canonical_work_key,
        provenance=dict(provenance or {}),
        requested_by=requested_by,
    )
    db.add(row)
    db.flush()
    _record_event(db, row, event_type=AgentWorkAssignmentEventType.created, detail={"requested_by": requested_by})
    _record_event(db, row, event_type=AgentWorkAssignmentEventType.agent_assigned, detail={"agent_id": str(agent_id), "role": role.value})

    for depends_on_id in depends_on_assignment_ids:
        db.add(AgentWorkAssignmentDependency(assignment_id=row.id, depends_on_assignment_id=depends_on_id, owner_id=owner_id))
    db.flush()

    _refresh_dependency_state(db, row)
    return row


def _dependency_ids(db: Session, assignment: AgentWorkAssignment) -> list[uuid.UUID]:
    rows = db.execute(
        select(AgentWorkAssignmentDependency.depends_on_assignment_id).where(
            AgentWorkAssignmentDependency.assignment_id == assignment.id, AgentWorkAssignmentDependency.owner_id == assignment.owner_id
        )
    ).all()
    return [row[0] for row in rows]


def _dependencies_satisfied(db: Session, assignment: AgentWorkAssignment) -> bool:
    dep_ids = _dependency_ids(db, assignment)
    if not dep_ids:
        return True
    deps = db.execute(select(AgentWorkAssignment).where(AgentWorkAssignment.id.in_(dep_ids))).scalars().all()
    return all(dep.status.value in DEPENDENCY_RELEASE_STATUSES for dep in deps)


def _refresh_dependency_state(db: Session, assignment: AgentWorkAssignment) -> None:
    if assignment.status not in (WorkAssignmentStatus.planned, WorkAssignmentStatus.waiting_dependency):
        return
    if _dependencies_satisfied(db, assignment):
        if assignment.status != WorkAssignmentStatus.ready:
            assignment.status = WorkAssignmentStatus.ready
            db.flush()
            _record_event(db, assignment, event_type=AgentWorkAssignmentEventType.dependency_satisfied)
    else:
        assignment.status = WorkAssignmentStatus.waiting_dependency
        db.flush()


def _release_dependents(db: Session, assignment: AgentWorkAssignment) -> None:
    rows = db.execute(
        select(AgentWorkAssignmentDependency.assignment_id).where(
            AgentWorkAssignmentDependency.depends_on_assignment_id == assignment.id,
            AgentWorkAssignmentDependency.owner_id == assignment.owner_id,
        )
    ).all()
    for (dependent_id,) in rows:
        dependent = db.get(AgentWorkAssignment, dependent_id)
        if dependent is not None:
            _refresh_dependency_state(db, dependent)


# ============================================================================ status machine

ALLOWED_TRANSITIONS: dict[WorkAssignmentStatus, frozenset[WorkAssignmentStatus]] = {
    WorkAssignmentStatus.planned: frozenset({WorkAssignmentStatus.ready, WorkAssignmentStatus.waiting_dependency, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.waiting_dependency: frozenset({WorkAssignmentStatus.ready, WorkAssignmentStatus.blocked, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.ready: frozenset({WorkAssignmentStatus.running, WorkAssignmentStatus.waiting_agent, WorkAssignmentStatus.blocked, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.waiting_agent: frozenset({WorkAssignmentStatus.running, WorkAssignmentStatus.blocked, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.running: frozenset({WorkAssignmentStatus.waiting_review, WorkAssignmentStatus.ready_for_review, WorkAssignmentStatus.blocked, WorkAssignmentStatus.failed, WorkAssignmentStatus.completed, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.waiting_review: frozenset({WorkAssignmentStatus.reviewing, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.ready_for_review: frozenset({WorkAssignmentStatus.reviewing, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.reviewing: frozenset({WorkAssignmentStatus.changes_requested, WorkAssignmentStatus.verified, WorkAssignmentStatus.completed, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.changes_requested: frozenset({WorkAssignmentStatus.running, WorkAssignmentStatus.ready, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.verified: frozenset({WorkAssignmentStatus.completed, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.blocked: frozenset({WorkAssignmentStatus.ready, WorkAssignmentStatus.running, WorkAssignmentStatus.waiting_dependency, WorkAssignmentStatus.failed, WorkAssignmentStatus.cancelled}),
    WorkAssignmentStatus.failed: frozenset({WorkAssignmentStatus.ready, WorkAssignmentStatus.superseded}),
    WorkAssignmentStatus.completed: frozenset({WorkAssignmentStatus.superseded}),
    WorkAssignmentStatus.cancelled: frozenset(),
    WorkAssignmentStatus.superseded: frozenset(),
}


def transition_status(
    db: Session, *, assignment: AgentWorkAssignment, new_status: str, detail: dict[str, Any] | None = None
) -> AgentWorkAssignment:
    current = assignment.status
    target = WorkAssignmentStatus(new_status)
    if target == current:
        return assignment  # idempotent replay -- see docs' "no silent guessing" note
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidTransitionError(f"{current.value} -> {target.value} is not an allowed transition")

    assignment.status = target
    now = datetime.utcnow()
    if target == WorkAssignmentStatus.running and assignment.started_at is None:
        assignment.started_at = now
    if target in TERMINAL_WORK_ASSIGNMENT_STATUSES:
        assignment.completed_at = now
    db.flush()
    _record_event(
        db, assignment, event_type=AgentWorkAssignmentEventType.status_changed,
        detail={"from": current.value, "to": target.value, **(detail or {})},
    )
    _release_dependents(db, assignment)
    return assignment


# ============================================================================ the conflict
# coordinator -- deterministic pre-flight readiness answers, never guessed.

OUTCOME_ASSIGNABLE = "ASSIGNABLE"
OUTCOME_PATH_CONFLICT = "PATH_CONFLICT"
OUTCOME_WORKTREE_CONFLICT = "WORKTREE_CONFLICT"
OUTCOME_DUPLICATE_WORK = "DUPLICATE_WORK"
OUTCOME_WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
OUTCOME_STALE_BASE = "STALE_BASE"
OUTCOME_AUTHORITY_CHANGED = "AUTHORITY_CHANGED"
OUTCOME_LEASE_REQUIRED = "LEASE_REQUIRED"
OUTCOME_AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"


@dataclass(frozen=True)
class CoordinatorDecision:
    outcome: str
    reason: str
    detail: dict = field(default_factory=dict)


def _active_leases_for_repo(db: Session, *, owner_id: uuid.UUID, repository_identity: str) -> list[AgentScopeLease]:
    return list(
        db.execute(
            select(AgentScopeLease).where(
                AgentScopeLease.owner_id == owner_id,
                AgentScopeLease.repository_identity == repository_identity,
                AgentScopeLease.status == AgentScopeLeaseStatus.active,
            )
        ).scalars()
    )


def _shares_parallel_exploration_group(db: Session, *, group_id: uuid.UUID | None, other_assignment_id: uuid.UUID) -> bool:
    """True iff `other_assignment_id` belongs to the SAME parallel-exploration group as
    `group_id` -- the one, explicit exemption from PATH_CONFLICT: two builders intentionally
    competing on the same canonical problem, each in their OWN isolated branch/worktree, are
    not "accidentally" overlapping just because their path scopes happen to overlap too. The
    WORKTREE_CONFLICT check is never exempted this way -- "they may not mutate the same
    physical worktree" holds even under PARALLEL_EXPLORATION."""

    if group_id is None:
        return False
    other = db.get(AgentWorkAssignment, other_assignment_id)
    return other is not None and other.parallel_exploration_group_id == group_id


def scan_write_scope_conflict(
    db: Session,
    *,
    owner_id: uuid.UUID,
    repository_identity: str,
    branch: str | None,
    worktree_path: str | None,
    allowed_paths: list[str] | tuple[str, ...],
    parallel_exploration_group_id: uuid.UUID | None = None,
    exclude_assignment_id: uuid.UUID | None = None,
) -> tuple[str | None, AgentScopeLease | None]:
    """Read-only scan for whether a `read_write` scope would conflict with any OTHER currently
    active write lease on the same repository -- the exact conflict rule
    `evaluate_assignment_readiness()`/`acquire_lease()` both enforce, factored out here so a
    caller that does not yet have an `AgentWorkAssignment` row
    (`app.agent_coordination.routing`'s eligibility check, run BEFORE an assignment exists) can
    ask the same question in advance, deterministically, without creating anything. Returns
    `(outcome, conflicting_lease)` where `outcome` is one of `OUTCOME_WORKTREE_CONFLICT`/
    `OUTCOME_PATH_CONFLICT`/`None`. Never locks, never mutates -- `acquire_lease()` takes its
    own `FOR UPDATE` lock separately at the moment it actually commits to a lease; this function
    is advisory only, exactly like `evaluate_assignment_readiness()`'s own read-only contract."""

    others = [
        lease
        for lease in _active_leases_for_repo(db, owner_id=owner_id, repository_identity=repository_identity)
        if lease.assignment_id != exclude_assignment_id
    ]
    if branch or worktree_path:
        for lease in others:
            if lease.branch == branch or lease.worktree_path == worktree_path:
                return OUTCOME_WORKTREE_CONFLICT, lease
    for lease in others:
        if lease.mode != WorkAssignmentReadWriteMode.read_write:
            continue
        if _shares_parallel_exploration_group(db, group_id=parallel_exploration_group_id, other_assignment_id=lease.assignment_id):
            continue
        if paths_conflict_any(allowed_paths, lease.allowed_paths):
            return OUTCOME_PATH_CONFLICT, lease
    return None, None


def evaluate_assignment_readiness(
    db: Session, *, assignment: AgentWorkAssignment, current_base_sha: str | None = None
) -> CoordinatorDecision:
    """Deterministically answers "can this assignment run right now" against everything Life
    already knows -- never a guess, never natural-language reasoning. Read-only: never mutates
    `assignment`."""

    available, reason = agent_availability(db, agent_id=assignment.agent_id)
    if not available:
        return CoordinatorDecision(OUTCOME_AGENT_UNAVAILABLE, reason)

    goal = db.get(MainAIGoal, assignment.goal_id)
    if goal is not None:
        recorded_hash = assignment.provenance.get("authorized_instruction_sha256")
        if recorded_hash is not None:
            live_hash = hashlib.sha256(goal.original_instruction.encode()).hexdigest()
            if live_hash != recorded_hash:
                return CoordinatorDecision(
                    OUTCOME_AUTHORITY_CHANGED, "goal.original_instruction no longer matches the authority this assignment was created under"
                )

    if current_base_sha is not None and assignment.base_sha is not None and current_base_sha != assignment.base_sha:
        return CoordinatorDecision(
            OUTCOME_STALE_BASE,
            f"assignment's recorded base_sha '{assignment.base_sha}' no longer matches the current base '{current_base_sha}'",
        )

    if not _dependencies_satisfied(db, assignment):
        return CoordinatorDecision(OUTCOME_WAITING_DEPENDENCY, "one or more dependency assignments have not reached a releasing status")

    duplicate = is_duplicate_canonical_work(
        db,
        owner_id=assignment.owner_id,
        canonical_work_key=assignment.canonical_work_key,
        parallel_exploration_group_id=assignment.parallel_exploration_group_id,
    )
    if duplicate is not None and duplicate.id != assignment.id:
        return CoordinatorDecision(OUTCOME_DUPLICATE_WORK, f"assignment {duplicate.id} already covers this canonical work")

    if assignment.read_write_mode == WorkAssignmentReadWriteMode.read_write:
        conflict_outcome, conflicting_lease = scan_write_scope_conflict(
            db,
            owner_id=assignment.owner_id,
            repository_identity=assignment.repository_identity,
            branch=assignment.branch,
            worktree_path=assignment.worktree_path,
            allowed_paths=assignment.allowed_paths,
            parallel_exploration_group_id=assignment.parallel_exploration_group_id,
            exclude_assignment_id=assignment.id,
        )
        if conflict_outcome == OUTCOME_WORKTREE_CONFLICT:
            return CoordinatorDecision(OUTCOME_WORKTREE_CONFLICT, f"lease {conflicting_lease.id} already holds this branch/worktree")
        if conflict_outcome == OUTCOME_PATH_CONFLICT:
            return CoordinatorDecision(OUTCOME_PATH_CONFLICT, f"lease {conflicting_lease.id}'s write scope overlaps this assignment's allowed_paths")

        own_lease = db.execute(
            select(AgentScopeLease).where(AgentScopeLease.assignment_id == assignment.id, AgentScopeLease.status == AgentScopeLeaseStatus.active)
        ).scalar_one_or_none()
        if own_lease is None and assignment.status in (WorkAssignmentStatus.ready, WorkAssignmentStatus.running):
            return CoordinatorDecision(OUTCOME_LEASE_REQUIRED, "a read_write assignment must hold an active scope lease before it can run")

    return CoordinatorDecision(OUTCOME_ASSIGNABLE, "no conflict, dependency, staleness, or availability blocker found")


# ============================================================================ scope leases

@dataclass(frozen=True)
class LeaseAcquisitionResult:
    outcome: str  # "ACQUIRED" | "WORKTREE_CONFLICT" | "PATH_CONFLICT"
    lease: AgentScopeLease | None
    reason: str


def acquire_lease(
    db: Session,
    *,
    assignment: AgentWorkAssignment,
    agent_id: uuid.UUID,
    branch: str,
    worktree_path: str,
    allowed_paths: list[str] | tuple[str, ...],
    mode: str,
    ttl_seconds: int | None = None,
) -> LeaseAcquisitionResult:
    """The write-conflict-prevention gate. READ_ONLY scopes never conflict with anything (rule
    1) and skip conflict scanning entirely. Idempotent replay: re-acquiring for the SAME
    assignment with an allowed_paths that is a SUBSET of what it already holds returns the
    existing row unchanged; a request to WIDEN the scope is rejected (ScopeExpansionError) --
    "a worker may never silently expand its own path scope.\""""

    mode = WorkAssignmentReadWriteMode(mode)
    existing = db.execute(
        select(AgentScopeLease)
        .where(AgentScopeLease.assignment_id == assignment.id, AgentScopeLease.status == AgentScopeLeaseStatus.active)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if not set(allowed_paths) <= set(existing.allowed_paths):
            raise ScopeExpansionError(
                f"assignment {assignment.id} already holds lease {existing.id} scoped to {existing.allowed_paths}; "
                f"{list(allowed_paths)} is not a subset -- scope expansion is never granted implicitly"
            )
        return LeaseAcquisitionResult("ACQUIRED", existing, "idempotent replay of an already-active lease")

    now = datetime.utcnow()
    if mode != WorkAssignmentReadWriteMode.read_only:
        locked_others = list(
            db.execute(
                select(AgentScopeLease)
                .where(
                    AgentScopeLease.owner_id == assignment.owner_id,
                    AgentScopeLease.repository_identity == assignment.repository_identity,
                    AgentScopeLease.status == AgentScopeLeaseStatus.active,
                )
                .order_by(AgentScopeLease.id)
                .with_for_update()
            ).scalars()
        )
        for lease in locked_others:
            if lease.branch == branch or lease.worktree_path == worktree_path:
                return LeaseAcquisitionResult("WORKTREE_CONFLICT", None, f"lease {lease.id} already holds branch/worktree")
        for lease in locked_others:
            if lease.mode != WorkAssignmentReadWriteMode.read_write:
                continue
            if _shares_parallel_exploration_group(db, group_id=assignment.parallel_exploration_group_id, other_assignment_id=lease.assignment_id):
                continue
            if paths_conflict_any(allowed_paths, lease.allowed_paths):
                return LeaseAcquisitionResult("PATH_CONFLICT", None, f"lease {lease.id}'s write scope overlaps the requested allowed_paths")

    lease = AgentScopeLease(
        owner_id=assignment.owner_id,
        assignment_id=assignment.id,
        agent_id=agent_id,
        repository_identity=assignment.repository_identity,
        branch=branch,
        worktree_path=worktree_path,
        allowed_paths=list(allowed_paths),
        mode=mode,
        lease_generation=1,
        status=AgentScopeLeaseStatus.active,
        acquired_at=now,
        expires_at=(now + timedelta(seconds=ttl_seconds)) if ttl_seconds else None,
        last_heartbeat_at=now if ttl_seconds else None,
    )
    db.add(lease)
    assignment.branch = branch
    assignment.worktree_path = worktree_path
    db.flush()
    _record_event(
        db, assignment, event_type=AgentWorkAssignmentEventType.lease_acquired,
        detail={"lease_id": str(lease.id), "branch": branch, "worktree_path": worktree_path, "mode": mode},
    )
    return LeaseAcquisitionResult("ACQUIRED", lease, "lease acquired")


def _load_lease(db: Session, lease_id: uuid.UUID, *, owner_id: uuid.UUID, lock: bool = False) -> AgentScopeLease:
    query = select(AgentScopeLease).where(AgentScopeLease.id == lease_id, AgentScopeLease.owner_id == owner_id)
    lease = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if lease is None:
        raise CoordinationError(f"AgentScopeLease {lease_id} does not exist for this owner")
    return lease


def renew_lease(
    db: Session, *, lease_id: uuid.UUID, owner_id: uuid.UUID, agent_id: uuid.UUID, lease_generation: int, ttl_seconds: int
) -> AgentScopeLease:
    lease = _load_lease(db, lease_id, owner_id=owner_id, lock=True)
    if lease.status != AgentScopeLeaseStatus.active:
        raise LeaseFencingError(f"lease {lease_id} is '{lease.status.value}', not active")
    if lease.agent_id != agent_id or lease.lease_generation != lease_generation:
        raise LeaseFencingError(f"lease {lease_id}: presented (agent_id, generation) does not match the current holder")
    if is_lease_stale(lease):
        raise LeaseFencingError(
            f"lease {lease_id} already expired at {lease.expires_at} -- a stale worker must lose write authority; "
            "use take_over_lease() instead of renewing past expiry"
        )
    now = datetime.utcnow()
    lease.last_heartbeat_at = now
    lease.expires_at = now + timedelta(seconds=ttl_seconds)
    db.flush()
    assignment = db.get(AgentWorkAssignment, lease.assignment_id)
    _record_event(db, assignment, event_type=AgentWorkAssignmentEventType.lease_renewed, detail={"lease_id": str(lease.id)})
    return lease


def is_lease_stale(lease: AgentScopeLease, *, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return lease.status == AgentScopeLeaseStatus.active and lease.expires_at is not None and lease.expires_at < now


def take_over_lease(
    db: Session,
    *,
    lease_id: uuid.UUID,
    owner_id: uuid.UUID,
    new_agent_id: uuid.UUID,
    new_assignment_id: uuid.UUID | None = None,
    expected_generation: int | None = None,
    ttl_seconds: int | None = None,
) -> AgentScopeLease:
    """Takeover REQUIRES the current lease to actually be stale (past its own `expires_at`) --
    "a stale worker must lose write authority" implies liveness is the gate, never mere
    contention (a live holder's lease can only ever be released by that holder). Bumps
    `lease_generation` by exactly 1, IN PLACE on the same row -- the old holder's own reference
    to the prior generation is naturally fenced out the moment it next presents that number to
    renew_lease()/release_lease(). `expected_generation`, if supplied, makes a concurrent
    double-takeover race deterministic: the second racer's expectation no longer matches the
    first racer's already-applied bump and it is rejected, not silently re-applied."""

    lease = _load_lease(db, lease_id, owner_id=owner_id, lock=True)
    if expected_generation is not None and lease.lease_generation != expected_generation:
        raise LeaseFencingError(f"lease {lease_id}: generation changed by a concurrent takeover (now {lease.lease_generation})")
    if lease.status != AgentScopeLeaseStatus.active:
        raise LeaseFencingError(f"lease {lease_id} is '{lease.status.value}', not active")
    if not is_lease_stale(lease):
        raise LeaseStillLiveError(f"lease {lease_id} is still live (expires_at={lease.expires_at}) -- takeover requires staleness")

    old_agent_id, old_generation = lease.agent_id, lease.lease_generation
    now = datetime.utcnow()
    lease.agent_id = new_agent_id
    if new_assignment_id is not None:
        lease.assignment_id = new_assignment_id
    lease.lease_generation += 1
    lease.acquired_at = now
    lease.last_heartbeat_at = now if ttl_seconds else None
    lease.expires_at = (now + timedelta(seconds=ttl_seconds)) if ttl_seconds else None
    db.flush()
    assignment = db.get(AgentWorkAssignment, lease.assignment_id)
    _record_event(
        db, assignment, event_type=AgentWorkAssignmentEventType.lease_taken_over,
        detail={
            "lease_id": str(lease.id), "old_agent_id": str(old_agent_id), "new_agent_id": str(new_agent_id),
            "old_generation": old_generation, "new_generation": lease.lease_generation,
        },
    )
    return lease


def release_lease(db: Session, *, lease_id: uuid.UUID, owner_id: uuid.UUID, agent_id: uuid.UUID, lease_generation: int) -> AgentScopeLease:
    lease = _load_lease(db, lease_id, owner_id=owner_id, lock=True)
    if lease.status != AgentScopeLeaseStatus.active:
        return lease  # already released/superseded -- idempotent no-op, the desired end state already holds
    if lease.agent_id != agent_id or lease.lease_generation != lease_generation:
        raise LeaseFencingError(f"lease {lease_id}: presented (agent_id, generation) does not match the current holder")
    lease.status = AgentScopeLeaseStatus.released
    lease.released_at = datetime.utcnow()
    db.flush()
    assignment = db.get(AgentWorkAssignment, lease.assignment_id)
    _record_event(db, assignment, event_type=AgentWorkAssignmentEventType.lease_released, detail={"lease_id": str(lease.id)})
    return lease


# ============================================================================ capability /
# performance evidence -- thin adapter over app.intelligence_governance, never a competing
# store. See that module's own docstring for record_execution()/record_evidence()'s
# idempotency guarantees, inherited unchanged here.

_ROLE_TO_INTELLIGENCE_ROLE = {
    WorkAssignmentRole.builder.value: "builder",
    WorkAssignmentRole.reviewer.value: "reviewer",
    WorkAssignmentRole.challenger.value: "challenger",
    # IntelligenceExecution.role is a small, DB-CHECK-constrained vocabulary
    # (builder/reviewer/challenger/verifier/planner/unknown) that predates this module's own
    # richer role set -- TESTER/RESEARCHER/SYNTHESIZER map to their closest existing bucket
    # here; the coordinator's OWN, real role is never lost, it is preserved verbatim in this
    # execution's `context` (see record_assignment_execution below), which is unconstrained
    # JSON. Deliberately not a migration to widen intelligence_executions' own CHECK
    # constraint -- this stays a reference/adapter, never a fork of that table's vocabulary.
    WorkAssignmentRole.tester.value: "verifier",
    WorkAssignmentRole.researcher.value: "planner",
    WorkAssignmentRole.synthesizer.value: "planner",
}


def record_assignment_execution(db: Session, *, assignment: AgentWorkAssignment, agent: CoordinationAgent) -> uuid.UUID:
    """Records ONE `IntelligenceExecution` row for this assignment (idempotent by
    `idempotency_key` -- calling this twice for the same assignment returns the SAME row) and
    stores its id back onto the assignment. This is the ONLY place capability/performance
    evidence enters this module's own tables -- as a foreign key reference, never a
    duplicated column."""

    if assignment.task_id is None:
        raise CoordinationError("record_assignment_execution requires assignment.task_id (IntelligenceExecution.task_id is NOT NULL)")
    execution = record_execution(
        db,
        owner_id=assignment.owner_id,
        task_id=assignment.task_id,
        idempotency_key=f"agent-coordination:{assignment.id}",
        agent_identity=str(agent.agent_key),
        model=agent.model_hint,
        execution_environment=agent.adapter_kind.value,
        capabilities=list(agent.capabilities),
        role=_ROLE_TO_INTELLIGENCE_ROLE.get(assignment.role.value, "unknown"),
        participation_mode="parallel" if assignment.parallel_exploration_group_id else "primary",
        context={"work_coordination_role": assignment.role.value, "assignment_id": str(assignment.id)},
        classification_basis="deterministic",
    )
    assignment.intelligence_execution_id = execution.id
    db.flush()
    _record_event(
        db, assignment, event_type=AgentWorkAssignmentEventType.evidence_recorded, detail={"intelligence_execution_id": str(execution.id)}
    )
    return execution.id


def record_assignment_outcome(
    db: Session,
    *,
    assignment: AgentWorkAssignment,
    evidence_kind: str,
    payload: dict[str, Any],
    review_kind: str = "unknown",
    deterministic: bool = False,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    if assignment.intelligence_execution_id is None:
        raise CoordinationError("record_assignment_outcome requires record_assignment_execution() to have run first")
    evidence = record_evidence(
        db,
        owner_id=assignment.owner_id,
        execution_id=assignment.intelligence_execution_id,
        evidence_kind=evidence_kind,
        payload=payload,
        source_type="agent_coordination",
        source_ref=str(assignment.id),
        idempotency_key=idempotency_key or f"agent-coordination-outcome:{assignment.id}:{evidence_kind}",
        review_kind=review_kind,
        deterministic=deterministic,
    )
    _record_event(db, assignment, event_type=AgentWorkAssignmentEventType.evidence_recorded, detail={"evidence_id": str(evidence.id), "evidence_kind": evidence_kind})
    return evidence.id


def build_agent_outcome_payload(
    *,
    tests_passed: bool | None = None,
    tests_run: int | None = None,
    tests_failed: int | None = None,
    duration_seconds: float | None = None,
    cost_tokens: int | None = None,
    cost_usd: float | None = None,
    ci_conclusion: str | None = None,
    review_defects_found: int | None = None,
    max_defect_severity: str | None = None,
    rework_required: bool | None = None,
    scope_violation: bool | None = None,
    merge_result: str | None = None,
    failure_reason: str | None = None,
    verified_quality: str | None = None,
) -> dict[str, Any]:
    """Canonical, documented payload vocabulary for `record_assignment_outcome()`'s `payload`
    argument -- a plain dict (`IntelligenceEvidence.payload` is unconstrained JSON, see that
    table's own docstring in migration 0038), never a new store or schema of its own. Exists so
    every caller records agent-outcome evidence using the SAME field names instead of each
    inventing its own ad hoc shape -- a future, evidence-driven Agent Capability Matrix would
    read exactly this vocabulary once there is enough of it accumulated to be meaningful (see
    docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's explicit "durable evidence foundation first,
    never a ranking engine built on insufficient data" note). Every field is optional and
    omitted entirely from the returned dict when not supplied -- a caller records only what it
    actually observed, never a fabricated placeholder for what it did not."""

    payload = {
        "tests_passed": tests_passed,
        "tests_run": tests_run,
        "tests_failed": tests_failed,
        "duration_seconds": duration_seconds,
        "cost_tokens": cost_tokens,
        "cost_usd": cost_usd,
        "ci_conclusion": ci_conclusion,
        "review_defects_found": review_defects_found,
        "max_defect_severity": max_defect_severity,
        "rework_required": rework_required,
        "scope_violation": scope_violation,
        "merge_result": merge_result,
        "failure_reason": failure_reason,
        "verified_quality": verified_quality,
    }
    return {key: value for key, value in payload.items() if value is not None}
