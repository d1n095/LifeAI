"""Read-only bridge from `AgentWorkAssignment` (WHO/WHAT, `app.agent_coordination`) to the
REAL settled dollar/token ledger, `ProviderSpendUsageEvent` (`app.provider_spend`) -- joined
via the assignment's own `goal_id`/`task_id`, exactly as
docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §0 documents: `provider_spend`
ties to `goal_id`/`task_id`/`job_id`, never to `agent_id`/`attempt_id`, and no join path
existed before this module. `provider_spend` remains the sole source of real dollar/token
truth -- this module never creates a parallel ledger, never calls `reserve_provider_spend_
call()`/`settle_provider_spend_call()` (mutating), and only ever reads `ProviderSpendUsageEvent`
rows already `settled`.

Also the first real populator+reader of `app.agent_coordination.service.
build_agent_outcome_payload()`'s existing `cost_tokens`/`cost_usd`/`duration_seconds` fields
(confirmed by the reconciliation doc to have zero real callers today) -- `populate_agent_
outcome_cost_fields()` calls that REAL function directly, never reimplements it."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.service import build_agent_outcome_payload
from app.models.agent_coordination import AgentDispatchExecution, AgentWorkAssignment, WorkAssignmentStatus
from app.models.provider_spend import ProviderSpendUsageEvent, ProviderSpendUsageStatus
from app.resource_intelligence.types import MetricEnvelope, unknown_metric

_SOURCE = "provider_spend_usage_events (settled)"

_ACCEPTED_STATUSES = frozenset({WorkAssignmentStatus.completed, WorkAssignmentStatus.verified})


def _as_decimal(value: Decimal | float | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _get_owned_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> AgentWorkAssignment | None:
    return db.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.id == assignment_id, AgentWorkAssignment.owner_id == owner_id)
    ).scalar_one_or_none()


def _settled_usage_events_for_scope(
    db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID, task_id: uuid.UUID | None, since: datetime | None = None
) -> list[ProviderSpendUsageEvent]:
    query = select(ProviderSpendUsageEvent).where(
        ProviderSpendUsageEvent.owner_id == owner_id,
        ProviderSpendUsageEvent.goal_id == goal_id,
        ProviderSpendUsageEvent.status == ProviderSpendUsageStatus.settled.value,
    )
    # task_id is the precise join key when the assignment carries one; when it does not
    # (AgentWorkAssignment.task_id is nullable), the join necessarily falls back to goal_id
    # alone -- coarser, disclosed via the caller's own `uncertainty` field, never silently
    # treated as exact.
    if task_id is not None:
        query = query.where(ProviderSpendUsageEvent.task_id == task_id)
    if since is not None:
        query = query.where(ProviderSpendUsageEvent.observed_at >= since)
    return list(db.execute(query).scalars())


def cost_for_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> MetricEnvelope:
    """Sum of settled `cost_usd` for `ProviderSpendUsageEvent` rows joined via this
    assignment's own `goal_id`/`task_id`. `missing_data=True` when the assignment does not
    exist for this owner, or when zero settled usage events match -- never silently reported
    as `$0.00` (a genuinely free/uncharged call would still produce a settled event with
    `cost_usd=0`; zero MATCHING EVENTS means no observation exists at all)."""

    definition = "sum of settled ProviderSpendUsageEvent.cost_usd joined via this assignment's goal_id/task_id"
    assignment = _get_owned_assignment(db, owner_id=owner_id, assignment_id=assignment_id)
    if assignment is None:
        return unknown_metric(unit="usd", definition=definition, source=_SOURCE, method="assignment_id not found for this owner_id")

    events = _settled_usage_events_for_scope(db, owner_id=owner_id, goal_id=assignment.goal_id, task_id=assignment.task_id)
    if not events:
        return unknown_metric(
            unit="usd", definition=definition, source=_SOURCE,
            method=f"zero settled provider_spend usage events matched goal_id={assignment.goal_id} task_id={assignment.task_id}",
        )

    total = sum((_as_decimal(e.cost_usd) for e in events), Decimal("0"))
    uncertainty = None
    if assignment.task_id is None:
        uncertainty = "assignment has no task_id; cost is attributed to the entire goal_id, which may include other assignments/tasks under the same goal"

    return MetricEnvelope(
        value=float(total),
        unit="usd",
        definition=definition,
        denominator=None,
        time_window=None,
        population=f"assignment_id={assignment_id}",
        sample_size=len(events),
        source=_SOURCE,
        method=f"sum(cost_usd) over {len(events)} settled usage event(s) matching goal_id={assignment.goal_id} task_id={assignment.task_id}",
        missing_data=False,
        uncertainty=uncertainty,
        last_updated=max(e.observed_at for e in events),
        trend=None,
    )


def tokens_for_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> dict[str, MetricEnvelope]:
    """`prompt_tokens`/`completion_tokens` sums over the same settled-usage-event join
    `cost_for_assignment()` uses. `missing_data=True` (for both keys) when the assignment does
    not exist or zero settled usage events match."""

    definitions = {
        "prompt_tokens": "sum of settled ProviderSpendUsageEvent.prompt_tokens joined via this assignment's goal_id/task_id",
        "completion_tokens": "sum of settled ProviderSpendUsageEvent.completion_tokens joined via this assignment's goal_id/task_id",
    }
    assignment = _get_owned_assignment(db, owner_id=owner_id, assignment_id=assignment_id)
    if assignment is None:
        return {key: unknown_metric(unit="tokens", definition=defn, source=_SOURCE, method="assignment_id not found for this owner_id") for key, defn in definitions.items()}

    events = _settled_usage_events_for_scope(db, owner_id=owner_id, goal_id=assignment.goal_id, task_id=assignment.task_id)
    if not events:
        method = f"zero settled provider_spend usage events matched goal_id={assignment.goal_id} task_id={assignment.task_id}"
        return {key: unknown_metric(unit="tokens", definition=defn, source=_SOURCE, method=method) for key, defn in definitions.items()}

    uncertainty = None
    if assignment.task_id is None:
        uncertainty = "assignment has no task_id; tokens are attributed to the entire goal_id, which may include other assignments/tasks under the same goal"
    last_updated = max(e.observed_at for e in events)

    def _envelope(key: str, total: int) -> MetricEnvelope:
        return MetricEnvelope(
            value=total,
            unit="tokens",
            definition=definitions[key],
            denominator=None,
            time_window=None,
            population=f"assignment_id={assignment_id}",
            sample_size=len(events),
            source=_SOURCE,
            method=f"sum({key}) over {len(events)} settled usage event(s) matching goal_id={assignment.goal_id} task_id={assignment.task_id}",
            missing_data=False,
            uncertainty=uncertainty,
            last_updated=last_updated,
            trend=None,
        )

    return {
        "prompt_tokens": _envelope("prompt_tokens", sum(e.prompt_tokens for e in events)),
        "completion_tokens": _envelope("completion_tokens", sum(e.completion_tokens for e in events)),
    }


def cost_per_accepted_commit(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID, since: datetime | None = None) -> MetricEnvelope:
    """Real settled cost across ALL of this agent's assignments (not just the accepted ones --
    an agent's wasted spend on failed/cancelled work still counts against its own cost-per-
    accepted-commit rate) divided by the count of this agent's assignments that reached
    `WorkAssignmentStatus.completed`/`verified` within `since` (or all-time when `since` is
    `None`). `missing_data=True` -- NEVER a `ZeroDivisionError`, never a silent `0` -- when
    there are zero completions in the window."""

    definition = "sum of settled provider_spend cost_usd across this agent's assignments, divided by the count of this agent's assignments reaching completed/verified in the window"
    assignments = list(
        db.execute(select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id, AgentWorkAssignment.agent_id == agent_id)).scalars()
    )
    if not assignments:
        return unknown_metric(unit="usd_per_accepted_commit", definition=definition, source=_SOURCE, method=f"agent_id={agent_id} has no assignments for this owner_id")

    completions = [
        a for a in assignments
        if a.status in _ACCEPTED_STATUSES and (since is None or (a.completed_at is not None and a.completed_at >= since))
    ]
    completion_count = len(completions)
    if completion_count == 0:
        return unknown_metric(
            unit="usd_per_accepted_commit", definition=definition, source=_SOURCE,
            method=f"0 of {len(assignments)} assignment(s) for agent_id={agent_id} reached completed/verified in the requested window",
        )

    # Deduplicate by usage event id across the agent's assignments -- two assignments can
    # legitimately share the same (goal_id, task_id) (e.g. a builder + reviewer role on the
    # same task), which would otherwise double-count the same settled spend.
    scope_pairs = {(a.goal_id, a.task_id) for a in assignments}
    unique_events: dict[uuid.UUID, ProviderSpendUsageEvent] = {}
    any_task_id_none = False
    for goal_id, task_id in scope_pairs:
        if task_id is None:
            any_task_id_none = True
        for event in _settled_usage_events_for_scope(db, owner_id=owner_id, goal_id=goal_id, task_id=task_id, since=since):
            unique_events[event.id] = event

    total_cost = sum((_as_decimal(e.cost_usd) for e in unique_events.values()), Decimal("0"))
    value = float(total_cost) / completion_count

    uncertainty = None
    if any_task_id_none:
        uncertainty = "at least one assignment has no task_id; its own cost contribution is attributed at the goal_id level, which may include other tasks under the same goal"

    return MetricEnvelope(
        value=value,
        unit="usd_per_accepted_commit",
        definition=definition,
        denominator="count of completed/verified assignments",
        time_window=f"since={since.isoformat()}" if since is not None else "all-time",
        population=f"agent_id={agent_id}",
        sample_size=completion_count,
        source=_SOURCE,
        method=f"total_cost=${total_cost} over {len(unique_events)} unique settled usage event(s) across {len(assignments)} assignment(s); / {completion_count} completed/verified assignment(s)",
        missing_data=False,
        uncertainty=uncertainty,
        last_updated=max((e.observed_at for e in unique_events.values()), default=datetime.utcnow()),
        trend=None,
    )


def _total_execution_seconds(executions: list[AgentDispatchExecution]) -> float | None:
    total = 0.0
    found_any = False
    for execution in executions:
        if execution.ended_at is not None:
            total += (execution.ended_at - execution.started_at).total_seconds()
            found_any = True
    return total if found_any else None


def populate_agent_outcome_cost_fields(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> dict[str, Any]:
    """Becomes the first real populator of `app.agent_coordination.service.
    build_agent_outcome_payload()`'s already-declared `cost_tokens`/`cost_usd`/
    `duration_seconds` fields (confirmed by the reconciliation doc to have zero real callers
    today) -- calls that REAL function directly (never reimplements its own dict-building
    logic), filling its three cost-related kwargs from this module's own bridge data. Every
    other field of that payload is left at its own default (omitted), exactly as an ordinary
    caller that only observed cost/duration would do.

    `cost_usd`/`cost_tokens` are omitted (left `None`) when the underlying `MetricEnvelope`s
    report `missing_data=True` -- this function never passes a fabricated `0` into the
    payload. `duration_seconds` is the sum of this assignment's own `AgentDispatchExecution`
    attempts' real `ended_at - started_at` spans (only counting attempts that actually
    finished); `None` when no attempt has ended yet."""

    cost_metric = cost_for_assignment(db, owner_id=owner_id, assignment_id=assignment_id)
    token_metrics = tokens_for_assignment(db, owner_id=owner_id, assignment_id=assignment_id)

    cost_usd = None if cost_metric.missing_data else cost_metric.value
    cost_tokens = None
    if not token_metrics["prompt_tokens"].missing_data and not token_metrics["completion_tokens"].missing_data:
        cost_tokens = int(token_metrics["prompt_tokens"].value) + int(token_metrics["completion_tokens"].value)

    executions = list(
        db.execute(
            select(AgentDispatchExecution)
            .where(AgentDispatchExecution.owner_id == owner_id, AgentDispatchExecution.assignment_id == assignment_id)
            .order_by(AgentDispatchExecution.started_at)
        ).scalars()
    )
    duration_seconds = _total_execution_seconds(executions)

    return build_agent_outcome_payload(
        cost_tokens=cost_tokens,
        cost_usd=cost_usd,
        duration_seconds=duration_seconds,
    )
