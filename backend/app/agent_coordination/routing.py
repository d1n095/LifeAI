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

from app.agent_coordination.service import agent_availability, scan_write_scope_conflict
from app.models.agent_coordination import CoordinationAgent, CoordinationAgentStatus, WorkAssignmentReadWriteMode, WorkAssignmentRole

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
            return RoutingDecision(
                OUTCOME_SCOPE_CONFLICT,
                reason=f"requested scope already conflicts with active lease {conflicting_lease.id} ({conflict_outcome})",
            )

    required = set(required_capabilities)
    candidates = list(db.execute(select(CoordinationAgent).order_by(CoordinationAgent.agent_key)).scalars())
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
