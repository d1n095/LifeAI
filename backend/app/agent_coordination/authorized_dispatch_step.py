"""Dispatch a selected authorized assignment to a REAL agent, or durably WAIT for a provider.

This is the supervisor step that follows task selection in Life's development-management loop
(GitHub evidence → select → **dispatch** → …). It composes EXISTING public `agent_coordination`
primitives only — no new queue, registry, approval, STOP or transport:

    transition_status (planned → ready)
      → evaluate_dispatch_readiness(require_adapter_enabled=True)   (the fail-closed gate:
          base readiness incl. lease/duplicate/conflict, capability, explicit write scope,
          REAL adapter availability, founder approval)
      → get_real_adapter(...)                                      (NotConfiguredAdapter when a
          provider is not founder-enabled/installed/argv-configured — never a fake)
      → dispatch_assignment(...)                                   (only when the gate passes;
          transitions ready → waiting_agent → running on a confirmed real start)

When the resolved provider is NOT genuinely available (not founder-enabled, or its executable
is not on PATH), the gate returns ADAPTER_DISABLED / ADAPTER_UNAVAILABLE and this step records a
durable **WAITING_FOR_PROVIDER** state (ready → blocked with a named missing capability) and
returns — the assignment is preserved for a later tick, other authorized work is unaffected, and
no external delivery is ever simulated. `resume_waiting_for_provider` moves it back to `ready`
once a provider becomes available. `first_cycle_go` stays false.

IMPORTANT: even when a provider is "enabled", a missing founder-supplied argv template yields
`NotConfiguredAdapter`, so `dispatch_assignment` fails closed to `blocked`
(REAL_PROVIDER_NOT_CONFIGURED) — a successful result is NEVER fabricated. A genuine external
round-trip requires a separately-reviewed real Agent Runtime, which this step calls but does
not itself provide.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agent_coordination.adapter_config import adapter_availability
from app.agent_coordination.adapters import get_real_adapter
from app.agent_coordination.dispatch import (
    OUTCOME_ADAPTER_DISABLED,
    OUTCOME_ADAPTER_UNAVAILABLE,
    dispatch_assignment,
    evaluate_dispatch_readiness,
)
from app.agent_coordination.service import OUTCOME_ASSIGNABLE, transition_status
from app.models.agent_coordination import AgentWorkAssignment, CoordinationAgent, WorkAssignmentStatus

WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
_PROVIDER_UNAVAILABLE_OUTCOMES = frozenset({OUTCOME_ADAPTER_DISABLED, OUTCOME_ADAPTER_UNAVAILABLE})


@dataclass(frozen=True)
class DispatchStepResult:
    outcome: str  # "dispatched" | "waiting_for_provider" | "gate_blocked"
    assignment_id: str
    agent_key: str
    provider_enabled: bool
    executable_found: bool
    dispatch_decision: str
    reason: str
    missing_capability: str | None = None
    delivery_claimed: bool = False
    first_cycle_go: bool = False

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "assignment_id": self.assignment_id,
            "agent_key": self.agent_key,
            "provider_enabled": self.provider_enabled,
            "executable_found": self.executable_found,
            "dispatch_decision": self.dispatch_decision,
            "reason": self.reason,
            "missing_capability": self.missing_capability,
            "delivery_claimed": self.delivery_claimed,
            "first_cycle_go": self.first_cycle_go,
        }


def dispatch_or_wait(
    db: Session,
    *,
    assignment: AgentWorkAssignment,
    agent: CoordinationAgent,
    required_capabilities: tuple[str, ...] = (),
    cwd: str = ".",
) -> DispatchStepResult:
    """Attempt a real dispatch of `assignment` to `agent`; durably WAIT if its provider is not
    genuinely available. Never simulates a delivery."""
    availability = adapter_availability(agent.agent_key)

    # Bring a freshly-planned assignment to `ready` so the dispatch gate can evaluate it.
    if assignment.status == WorkAssignmentStatus.planned:
        transition_status(db, assignment=assignment, new_status="ready", detail={"reason": "ready_for_dispatch"})

    decision = evaluate_dispatch_readiness(
        db,
        assignment=assignment,
        agent=agent,
        required_capabilities=required_capabilities,
        require_adapter_enabled=True,
    )

    # Provider genuinely unavailable → durable WAITING_FOR_PROVIDER (preserve, do not dispatch).
    if decision.outcome in _PROVIDER_UNAVAILABLE_OUTCOMES:
        if assignment.status == WorkAssignmentStatus.ready:
            transition_status(
                db,
                assignment=assignment,
                new_status="blocked",
                detail={
                    "reason": WAITING_FOR_PROVIDER,
                    "provider": agent.agent_key,
                    "missing_capability": decision.outcome,
                    "adapter_reason": availability.reason,
                },
            )
        return DispatchStepResult(
            outcome="waiting_for_provider",
            assignment_id=str(assignment.id),
            agent_key=agent.agent_key,
            provider_enabled=availability.enabled,
            executable_found=availability.executable_found,
            dispatch_decision=decision.outcome,
            reason=availability.reason,
            missing_capability=decision.outcome,
        )

    # Some other fail-closed gate stop (approval/capability/scope/lease/duplicate) — do not dispatch.
    if decision.outcome != OUTCOME_ASSIGNABLE:
        return DispatchStepResult(
            outcome="gate_blocked",
            assignment_id=str(assignment.id),
            agent_key=agent.agent_key,
            provider_enabled=availability.enabled,
            executable_found=availability.executable_found,
            dispatch_decision=decision.outcome,
            reason=decision.reason,
        )

    # Gate passed and provider is enabled+found: obtain the REAL adapter and dispatch for real.
    # get_real_adapter returns NotConfiguredAdapter unless an argv template is also configured,
    # so an incompletely-configured provider fails closed to blocked — never a fabricated result.
    adapter = get_real_adapter(agent.agent_key, cwd=cwd)
    dispatch_decision = asyncio.run(
        dispatch_assignment(
            db,
            assignment=assignment,
            agent=agent,
            adapter=adapter,
            required_capabilities=required_capabilities,
            require_adapter_enabled=True,
        )
    )
    dispatched = dispatch_decision.outcome == OUTCOME_ASSIGNABLE
    return DispatchStepResult(
        outcome="dispatched" if dispatched else "gate_blocked",
        assignment_id=str(assignment.id),
        agent_key=agent.agent_key,
        provider_enabled=availability.enabled,
        executable_found=availability.executable_found,
        dispatch_decision=dispatch_decision.outcome,
        reason=dispatch_decision.reason,
    )


def resume_waiting_for_provider(db: Session, *, assignment: AgentWorkAssignment) -> bool:
    """Move a WAITING_FOR_PROVIDER assignment back to `ready` so a later tick can retry dispatch
    once a provider becomes available. Returns True if it resumed one."""
    if assignment.status != WorkAssignmentStatus.blocked:
        return False
    transition_status(db, assignment=assignment, new_status="ready", detail={"reason": "provider_retry"})
    return True
