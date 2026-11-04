"""Continue vs Hand Off. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

CONTEXT ALREADY LOADED HAS VALUE. HANDOFF HAS COST. INTERRUPTION HAS COST. A nominally
stronger, cold agent does not automatically beat an agent already mid-task with loaded
context; a cheap agent with high rework does not beat an expensive, reliable one.

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_workforce.types import WaitOrAssignDecision

# Hand-picked, documented: the minimum competency edge a cold, nominally-stronger agent must
# have over the current agent's OWN net-of-rework quality before a handoff (which always pays
# `handoff_cost`) is worth it at all.
MIN_NET_COMPETENCY_EDGE_TO_HANDOFF = 0.15


@dataclass(frozen=True)
class ContinuationResult:
    decision: WaitOrAssignDecision
    reason: str


def decide_continue_or_handoff(
    *,
    current_agent_context_loaded_relevance: float,
    current_agent_competency: float,
    current_agent_rework_rate: float,
    candidate_agent_competency: float,
    candidate_agent_rework_rate: float,
    handoff_cost_seconds: float,
    interruption_cost_seconds: float = 0.0,
) -> ContinuationResult:
    """Net quality nets rework OUT of raw competency for both agents (a cheap agent with high
    rework can look strong on paper and still lose): `net = competency * (1 - rework_rate)`.
    The candidate must beat the current agent's net quality by more than
    `MIN_NET_COMPETENCY_EDGE_TO_HANDOFF` scaled down by the loaded-context value AND still be
    worth the real handoff + interruption cost, or continuation wins."""

    current_net = current_agent_competency * (1.0 - current_agent_rework_rate)
    candidate_net = candidate_agent_competency * (1.0 - candidate_agent_rework_rate)

    # Loaded context raises the bar a candidate must clear: fully-loaded relevance (1.0) adds
    # the full MIN_NET_COMPETENCY_EDGE_TO_HANDOFF on top of the base requirement; zero loaded
    # context means the base requirement alone applies.
    required_edge = MIN_NET_COMPETENCY_EDGE_TO_HANDOFF * (1.0 + current_agent_context_loaded_relevance)

    if candidate_net - current_net < required_edge:
        return ContinuationResult(
            WaitOrAssignDecision.CONTINUE_CURRENT_WORK,
            f"current agent's net quality ({current_net:.2f}) is not meaningfully beaten by the candidate ({candidate_net:.2f}); "
            f"CONTEXT ALREADY LOADED HAS VALUE (relevance={current_agent_context_loaded_relevance:.2f}) and HANDOFF HAS COST",
        )

    total_switch_cost = handoff_cost_seconds + interruption_cost_seconds
    if total_switch_cost > 0 and (candidate_net - current_net) < (total_switch_cost / 3600.0):
        return ContinuationResult(
            WaitOrAssignDecision.CONTINUE_CURRENT_WORK,
            f"candidate's edge ({candidate_net - current_net:.2f}) does not clear the real handoff+interruption cost ({total_switch_cost:.0f}s)",
        )

    return ContinuationResult(
        WaitOrAssignDecision.HAND_OFF,
        f"candidate's net quality ({candidate_net:.2f}) meaningfully beats the current agent's ({current_net:.2f}) even after accounting for loaded-context value and switch cost",
    )
