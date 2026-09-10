"""MainAI Resource Intelligence -- Part 2: `next_best_resource_allocation()`, the read-only
composition layer. See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §1.7 for
the architecture decision this module implements.

Composes `app.agent_coordination.runtime_view.all_agents_runtime_snapshot()` (the real, live
WIP/availability view -- "who is doing what, where, right now") with per-assignment
`decision.propose_resource_action()` calls, fed by this package's own real Part 1 telemetry/
cost_bridge data and Part 2's own `efficiency_profile.agent_efficiency_profile()`, to answer
"who should get attention next" -- ranked, advisory, read-only.

This function DOES take a `db: Session` (it is the composition layer, not the pure core
`decision.py` is) but it never calls any MUTATING function itself: read-only against
`app.agent_coordination` (`all_agents_runtime_snapshot()` plus direct `SELECT`s of
`AgentDispatchExecution` for the real `attempt_id` correlation, exactly like
`app.resource_intelligence.efficiency_profile` already does), read-only against this package's
own Part 1 functions, and it never touches `app.provider_spend` directly at all (that happens
one layer down, inside `cost_bridge`/`efficiency_profile`, both already proven read-only). It
never calls `create_work_assignment()`, `authorize_execution_scope()`, `transition_status()`, any
mutating `provider_spend`/`workforce.cost` function, or anything that actually compacts/resets/
hands off/reassigns a real session -- see this module's own structural test
(`tests/backend/mainai/test_resource_intelligence_scheduler.py`) for the source-regex proof.

Priority scoring reuses `app.mainai_executive.priority.score_priority()`'s own SCORING
DISCIPLINE (a documented, hand-picked weighted sum with a soft cap against one factor
dominating) rather than importing that exact function -- the factor set here genuinely differs
(action severity + context urgency + blocked-time pressure, not urgency/importance/founder_value
/etc), per this round's own brief."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.runtime_view import RuntimeStatus, all_agents_runtime_snapshot
from app.models.agent_coordination import AgentDispatchExecution
from app.resource_intelligence.decision import propose_resource_action
from app.resource_intelligence.efficiency_profile import agent_efficiency_profile
from app.resource_intelligence.telemetry import (
    context_utilization,
    estimated_time_to_context_limit,
    idle_productive_blocked_time,
)
from app.resource_intelligence.types import ContextLifecycleAction, unknown_metric

# Runtime statuses that mean an assignment is actually occupying attention right now -- IDLE
# (planned/ready/waiting_agent) has no session/telemetry to reason about yet, and COMPLETED/
# FAILED are terminal (nothing left to allocate attention to); skip both.
_IN_FLIGHT_RUNTIME_STATUSES = frozenset(
    {
        RuntimeStatus.RUNNING,
        RuntimeStatus.REVIEWING,
        RuntimeStatus.WAITING_REVIEW,
        RuntimeStatus.WAITING_DEPENDENCY,
        RuntimeStatus.BLOCKED,
    }
)

# Base severity per recommended action -- how urgently a human/harness should look at this
# allocation, independent of the exact metric values that produced it. Documented, hand-picked
# (same convention as decision.py's own thresholds): actions that risk losing state or require an
# agent swap rank above routine continuation; CONTINUE_CURRENT_SESSION ranks lowest because it is
# this module's own "nothing to see here" default.
_ACTION_SEVERITY: dict[ContextLifecycleAction, float] = {
    ContextLifecycleAction.CHECKPOINT: 1.0,
    ContextLifecycleAction.RESET_SESSION: 0.95,
    ContextLifecycleAction.HANDOFF: 0.85,
    ContextLifecycleAction.COMPACT: 0.8,
    ContextLifecycleAction.CHANGE_MODEL: 0.7,
    ContextLifecycleAction.CHANGE_PROVIDER: 0.7,
    ContextLifecycleAction.SPLIT_JOB: 0.6,
    ContextLifecycleAction.MOVE_SUBTASK: 0.55,
    ContextLifecycleAction.DEFER: 0.4,
    ContextLifecycleAction.KEEP_CURRENT_AGENT: 0.3,
    ContextLifecycleAction.CONTINUE_CURRENT_SESSION: 0.1,
}


def _latest_attempt_id(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> uuid.UUID | None:
    return db.execute(
        select(AgentDispatchExecution.attempt_id)
        .where(AgentDispatchExecution.owner_id == owner_id, AgentDispatchExecution.assignment_id == assignment_id)
        .order_by(AgentDispatchExecution.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _priority_score(
    *, action: ContextLifecycleAction, context_util_value: float | None, blocked_value: float | None, productive_value: float | None
) -> tuple[float, dict[str, Any]]:
    """Weighted sum + soft cap, matching `score_priority()`'s own discipline:

    `score = 0.6 * action_severity + 0.25 * context_component + 0.15 * blocked_component`

    - `action_severity`: `_ACTION_SEVERITY[action]`, the dominant term -- WHAT was recommended
      matters more than the raw numbers behind it.
    - `context_component`: `context_utilization / 100` when known, else `0.0` (an unknown
      context reading contributes no urgency of its own -- it is not assumed either healthy or
      concerning).
    - `blocked_component`: `blocked_seconds / (blocked_seconds + productive_seconds)` when both
      are known and their sum is positive (the fraction of observed time this assignment spent
      blocked rather than productive), else `0.0`.

    Soft cap (mirrors `score_priority()`'s own "confidence cannot alone push to NOW"): when
    `context_util_value` is unknown, the blocked component alone cannot push the total score
    above 0.5 -- an assignment with no real context reading should never outrank a genuinely
    acute one purely because it happened to have a high blocked-time fraction on record."""

    context_component = (context_util_value / 100.0) if context_util_value is not None else 0.0
    blocked_component = 0.0
    if blocked_value is not None and productive_value is not None and (blocked_value + productive_value) > 0:
        blocked_component = blocked_value / (blocked_value + productive_value)

    action_severity = _ACTION_SEVERITY.get(action, 0.3)
    score = 0.6 * action_severity + 0.25 * context_component + 0.15 * blocked_component

    capped = False
    if context_util_value is None and score > 0.5:
        score = 0.5
        capped = True

    return score, {
        "action_severity": action_severity,
        "context_component": round(context_component, 4),
        "blocked_component": round(blocked_component, 4),
        "context_unknown_soft_capped": capped,
    }


def next_best_resource_allocation(db: Session, *, owner_id: uuid.UUID) -> list[dict[str, Any]]:
    """Read-only, advisory ranking of "who should get the next look" across every registered
    agent's currently IN-FLIGHT assignment (see `_IN_FLIGHT_RUNTIME_STATUSES`). One row per
    in-flight assignment (an agent whose `concurrency_limit` exceeds 1 can contribute more than
    one row). Sorted descending by `priority_score` -- the assignment most in need of attention
    first.

    `wip_at_limit` is computed per-agent as `len(agent.current_assignments) >=
    agent.concurrency_limit` -- the same real `AgentRuntimeView.current_assignments`/
    `concurrency_limit` fields `app.agent_coordination.runtime_view` already exposes, never a
    second WIP concept.

    `critical_unsummarized_state`/`task_remaining_size` are NOT derivable from any real signal
    this composition layer has access to today (no caller-supplied summarization-state or
    remaining-work-size signal exists anywhere in this codebase yet) -- both are left at their
    honest defaults (`False`/`None`) rather than guessed from a proxy. A future caller with that
    real information can call `decision.propose_resource_action()` directly with it; this
    scheduler-level composition is a known, documented limitation, not a silent gap."""

    rows: list[dict[str, Any]] = []
    for agent_view in all_agents_runtime_snapshot(db, owner_id=owner_id):
        wip_at_limit = len(agent_view.current_assignments) >= agent_view.concurrency_limit

        for assignment_view in agent_view.current_assignments:
            if assignment_view.runtime_status not in _IN_FLIGHT_RUNTIME_STATUSES:
                continue

            attempt_id = _latest_attempt_id(db, owner_id=owner_id, assignment_id=assignment_view.assignment_id)
            if attempt_id is not None:
                context_util = context_utilization(db, owner_id=owner_id, attempt_id=attempt_id)
                time_to_limit = estimated_time_to_context_limit(db, owner_id=owner_id, attempt_id=attempt_id)
            else:
                context_util = unknown_metric(
                    unit="percent", definition="latest observed context_used_tokens / context_window_tokens, expressed as a percent",
                    source="agent_dispatch_executions", method=f"no AgentDispatchExecution row found for assignment_id={assignment_view.assignment_id}",
                )
                time_to_limit = unknown_metric(
                    unit="seconds", definition="projected wall-clock seconds until context_used_tokens reaches context_window_tokens",
                    source="agent_dispatch_executions", method=f"no AgentDispatchExecution row found for assignment_id={assignment_view.assignment_id}",
                )

            time_buckets = idle_productive_blocked_time(db, owner_id=owner_id, assignment_id=assignment_view.assignment_id)
            idle_seconds = time_buckets["idle_seconds"]
            productive_seconds = time_buckets["productive_seconds"]
            blocked_seconds = time_buckets["blocked_seconds"]

            profile = agent_efficiency_profile(db, owner_id=owner_id, agent_id=agent_view.agent_id, task_type=assignment_view.role)

            recommendation = propose_resource_action(
                context_utilization=context_util,
                time_to_limit=time_to_limit,
                idle_seconds=idle_seconds,
                productive_seconds=productive_seconds,
                blocked_seconds=blocked_seconds,
                efficiency_profile=profile,
                wip_at_limit=wip_at_limit,
                task_remaining_size=None,
                critical_unsummarized_state=False,
            )

            score, score_components = _priority_score(
                action=recommendation.action,
                context_util_value=context_util.value if not context_util.missing_data else None,
                blocked_value=blocked_seconds.value if not blocked_seconds.missing_data else None,
                productive_value=productive_seconds.value if not productive_seconds.missing_data else None,
            )

            rows.append(
                {
                    "agent_id": str(agent_view.agent_id),
                    "agent_key": agent_view.agent_key,
                    "assignment_id": str(assignment_view.assignment_id),
                    "canonical_status": assignment_view.canonical_status,
                    "runtime_status": assignment_view.runtime_status.value,
                    "role": assignment_view.role,
                    "wip_at_limit": wip_at_limit,
                    "recommendation": {
                        "action": recommendation.action.value,
                        "reason": recommendation.reason,
                        "signals": recommendation.signals,
                        "authorized": recommendation.authorized,
                    },
                    "priority_score": round(score, 4),
                    "priority_components": score_components,
                }
            )

    rows.sort(key=lambda row: row["priority_score"], reverse=True)
    return rows
