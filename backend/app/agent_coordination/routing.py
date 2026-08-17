"""Deterministic Agent Routing Foundation -- answers "which currently registered agent(s) are
eligible for this assignment" as a pure filter over what Life already knows (registry status,
availability, declared capability, write authority, scope conflict). Never performance
evidence -- that data does not exist in useful quantity yet (see
docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's explicit "durable evidence foundation first, never
a ranking engine built on insufficient data" note). Never provider prose. Never an autonomous
choice among multiple equally-eligible candidates.

This is eligibility FILTERING, not selection authority. A caller (the founder, or a future
Life orchestration loop operating under founder-set policy) makes the final choice;
`NEEDS_SELECTION` is the explicit, honest answer when more than one candidate remains equally
eligible after every filter -- this module never guesses a winner, and provider identity or
"trusted-sounding" agent names grant nothing (the same invariant
app.agent_coordination.service's own `test_provider_identity_cannot_grant_authority` already
proves at the assignment layer)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.service import (
    OUTCOME_ASSIGNABLE,
    OUTCOME_LEASE_REQUIRED,
    agent_availability,
    evaluate_assignment_readiness,
    scan_write_scope_conflict,
)
from app.models.agent_coordination import (
    AgentWorkAssignment,
    CoordinationAgent,
    CoordinationAgentStatus,
    WorkAssignmentReadWriteMode,
    WorkAssignmentRole,
    WorkAssignmentStatus,
)

OUTCOME_ELIGIBLE = "ELIGIBLE"
OUTCOME_NEEDS_SELECTION = "NEEDS_SELECTION"
OUTCOME_NONE_AVAILABLE = "NONE_AVAILABLE"
OUTCOME_SCOPE_CONFLICT = "SCOPE_CONFLICT"

# A REVIEWER/RESEARCHER may only ever be routed read_only -- the same invariant
# app.agent_coordination.service.create_work_assignment already enforces at creation time;
# checked again here so an ineligible role/mode pairing is never even offered as a routing
# candidate.
_REVIEW_ONLY_ROLES = frozenset({WorkAssignmentRole.reviewer, WorkAssignmentRole.researcher})


@dataclass(frozen=True)
class CandidateRejection:
    agent_id: uuid.UUID
    agent_key: str
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    outcome: str
    eligible_agent_ids: tuple[uuid.UUID, ...] = ()
    rejected: tuple[CandidateRejection, ...] = field(default_factory=tuple)
    reason: str = ""


def eligible_agents_for(
    db: Session,
    *,
    owner_id: uuid.UUID,
    role: str,
    read_write_mode: str,
    repository_identity: str,
    allowed_paths: list[str] | tuple[str, ...],
    required_capabilities: tuple[str, ...] = (),
    parallel_exploration_group_id: uuid.UUID | None = None,
) -> RoutingDecision:
    """Deterministic eligibility filter, applied in this fixed order:

    1. role/mode invariant (a reviewer/researcher may only be requested read_only)
    2. scope conflict (`scan_write_scope_conflict()` -- skipped entirely for read_only,
       exactly like `acquire_lease()`'s own "rule 1"; a conflicting scope blocks ALL agents,
       not a specific one, so this is checked once up front rather than per candidate)
    3. per-candidate: registry status (`active`)
    4. per-candidate: read/write capability (`supports_write`/`supports_read`)
    5. per-candidate: required capability tags (subset of the agent's own declared
       `capabilities`)
    6. per-candidate: availability (`agent_availability()` -- registry status + concurrency)

    Every rejection is recorded with its concrete reason (`rejected`) -- never silently
    dropped. Ties (2+ agents passing every filter) return `NEEDS_SELECTION`: this function
    picks no winner among equally eligible candidates -- see module docstring."""

    role_enum = WorkAssignmentRole(role)
    mode_enum = WorkAssignmentReadWriteMode(read_write_mode)
    if role_enum in _REVIEW_ONLY_ROLES and mode_enum != WorkAssignmentReadWriteMode.read_only:
        return RoutingDecision(
            OUTCOME_NONE_AVAILABLE, reason=f"role '{role}' must be assigned read_write_mode='read_only'; '{read_write_mode}' is not a valid pairing"
        )

    candidates = list(db.execute(select(CoordinationAgent).order_by(CoordinationAgent.agent_key)).scalars())

    if mode_enum == WorkAssignmentReadWriteMode.read_write:
        conflict_outcome, conflicting_lease = scan_write_scope_conflict(
            db,
            owner_id=owner_id,
            repository_identity=repository_identity,
            branch=None,
            worktree_path=None,
            allowed_paths=allowed_paths,
            parallel_exploration_group_id=parallel_exploration_group_id,
        )
        if conflict_outcome is not None:
            # The conflict is over the SCOPE itself, not any one candidate -- it blocks every
            # agent equally, so every candidate is recorded as rejected with the SAME concrete
            # reason (never left empty; a caller inspecting `rejected` for diagnostics on the
            # most common real-world rejection must not get nothing back).
            conflict_reason = f"requested scope already conflicts with active lease {conflicting_lease.id} ({conflict_outcome})"
            return RoutingDecision(
                OUTCOME_SCOPE_CONFLICT,
                rejected=tuple(CandidateRejection(a.id, a.agent_key, conflict_reason) for a in candidates),
                reason=conflict_reason,
            )

    required = set(required_capabilities)
    eligible: list[uuid.UUID] = []
    rejected: list[CandidateRejection] = []

    for agent in candidates:
        if agent.status != CoordinationAgentStatus.active:
            rejected.append(CandidateRejection(agent.id, agent.agent_key, f"registry status is '{agent.status.value}', not 'active'"))
            continue
        if mode_enum == WorkAssignmentReadWriteMode.read_write and not agent.supports_write:
            rejected.append(CandidateRejection(agent.id, agent.agent_key, "agent does not support write access"))
            continue
        if mode_enum == WorkAssignmentReadWriteMode.read_only and not agent.supports_read:
            rejected.append(CandidateRejection(agent.id, agent.agent_key, "agent does not support read access"))
            continue
        if required and not required.issubset(set(agent.capabilities)):
            missing = sorted(required - set(agent.capabilities))
            rejected.append(CandidateRejection(agent.id, agent.agent_key, f"missing required capabilities: {missing}"))
            continue
        available, reason = agent_availability(db, agent_id=agent.id)
        if not available:
            rejected.append(CandidateRejection(agent.id, agent.agent_key, reason))
            continue
        eligible.append(agent.id)

    if not eligible:
        return RoutingDecision(
            OUTCOME_NONE_AVAILABLE, rejected=tuple(rejected), reason="no registered agent passed every eligibility filter"
        )
    if len(eligible) > 1:
        return RoutingDecision(
            OUTCOME_NEEDS_SELECTION,
            eligible_agent_ids=tuple(eligible),
            rejected=tuple(rejected),
            reason="more than one agent is equally eligible -- selection authority is not this function's",
        )
    return RoutingDecision(OUTCOME_ELIGIBLE, eligible_agent_ids=tuple(eligible), rejected=tuple(rejected), reason="exactly one eligible agent")


# ============================================================================ the inverse
# direction -- given a KNOWN agent (typically one that just went idle, or is being polled by a
# caller deciding what it should do next), which of ITS OWN already-assigned, non-terminal work
# is actually feasible RIGHT NOW. `eligible_agents_for()` above answers "given work, which
# agent"; this answers "given an agent, which work" -- the two directions of the same matching
# problem, deliberately kept as separate, composable, single-purpose functions rather than one
# do-everything entry point.


@dataclass(frozen=True)
class NextAssignmentDecision:
    outcome: str
    assignment_id: uuid.UUID | None = None
    skipped: tuple[tuple[uuid.UUID, str], ...] = ()  # (assignment_id, CoordinatorDecision.outcome) for anything passed over
    reason: str = ""


OUTCOME_ASSIGNMENT_FOUND = "ASSIGNMENT_FOUND"
OUTCOME_NO_FEASIBLE_ASSIGNMENT = "NO_FEASIBLE_ASSIGNMENT"


def next_feasible_assignment_for_agent(db: Session, *, agent_id: uuid.UUID, owner_id: uuid.UUID) -> NextAssignmentDecision:
    """Deterministically selects the next assignment this agent should pick up, from among its
    OWN already-assigned (`AgentWorkAssignment.agent_id == agent_id`) `ready` work for this
    owner -- never work assigned to a DIFFERENT agent (this module never reassigns; that would
    be a silent authority transfer `create_work_assignment()`'s own caller never granted).

    Ordering is strict FIFO by `created_at` -- the oldest `ready` assignment wins. No priority
    heuristic, no capability-fit scoring, no performance-based preference: building any of that
    now would be exactly the "ranking engine built on insufficient data" this module's own
    docstring already refuses to build. A future, evidence-driven ordering is possible once
    there is real accumulated evidence (`app.agent_coordination.service.build_agent_outcome_payload`);
    this function stays FIFO until that day, deliberately.

    Scans candidates in order and returns the FIRST one `evaluate_assignment_readiness()` calls
    `ASSIGNABLE` -- OR `LEASE_REQUIRED`. `LEASE_REQUIRED` is deliberately treated as "found,"
    not "skip": for a fresh `read_write` `ready` assignment, `evaluate_assignment_readiness()`
    ALWAYS reports `LEASE_REQUIRED` until a lease is acquired (see that function's own final
    check) -- that is not a real blocker, it is simply naming the caller's own very next step
    (`acquire_lease()`), the same step selecting this assignment was already going to lead to.
    Treating it as a skip would make this function report `NO_FEASIBLE_ASSIGNMENT` for nearly
    every fresh write assignment ever created, which is not what "feasible" means here.

    A genuinely stuck `ready` assignment (e.g. `STALE_BASE`, a duplicate that surfaced after
    creation, `AGENT_UNAVAILABLE`) does NOT disqualify every other `ready` assignment behind it
    in the queue -- every one skipped over is recorded in `skipped`, never silently dropped.
    This is exactly what lets "one blocked assignment must not freeze unrelated work" hold from
    an AGENT's own point of view, not just the coordinator's.

    Never mutates anything -- no lease is acquired, no status is transitioned. Selecting a next
    assignment and actually starting it (`acquire_lease()` + `transition_status(...,
    new_status="running")`) are separate, deliberate caller actions; this function only
    answers "which one," never "go.\""""

    candidates = list(
        db.execute(
            select(AgentWorkAssignment)
            .where(
                AgentWorkAssignment.agent_id == agent_id,
                AgentWorkAssignment.owner_id == owner_id,
                AgentWorkAssignment.status == WorkAssignmentStatus.ready,
            )
            .order_by(AgentWorkAssignment.created_at)
        ).scalars()
    )

    if not candidates:
        return NextAssignmentDecision(OUTCOME_NO_FEASIBLE_ASSIGNMENT, reason="agent has no 'ready' assignments for this owner")

    skipped: list[tuple[uuid.UUID, str]] = []
    for assignment in candidates:
        decision = evaluate_assignment_readiness(db, assignment=assignment)
        if decision.outcome in (OUTCOME_ASSIGNABLE, OUTCOME_LEASE_REQUIRED):
            return NextAssignmentDecision(
                OUTCOME_ASSIGNMENT_FOUND, assignment_id=assignment.id, skipped=tuple(skipped),
                reason=f"oldest ready assignment that is actually feasible right now (skipped {len(skipped)} ahead of it)",
            )
        skipped.append((assignment.id, decision.outcome))

    return NextAssignmentDecision(
        OUTCOME_NO_FEASIBLE_ASSIGNMENT, skipped=tuple(skipped),
        reason=f"agent has {len(candidates)} 'ready' assignment(s) but none are assignable right now",
    )
