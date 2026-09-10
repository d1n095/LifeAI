"""Agent resource telemetry -- durable per-attempt samples (migration 0071), plus DERIVED
(never stored) idle/productive/blocked-time classification computed at read time from the
REAL `AgentDispatchExecution`/`AgentWorkAssignmentEvent` history. See
docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md for the full architecture
decision and app.models.resource_intelligence.AgentResourceTelemetrySample's own docstring for
why this table carries no deny-mutation trigger.

DERIVE, NEVER DUPLICATE: this module never stores a second heartbeat/liveness/status source of
truth -- `AgentRuntimeView.heartbeat_at` (app.agent_coordination.runtime_view) already
establishes that convention for this codebase; idle/productive/blocked time here is computed
the same way, from the real, already-durable `AgentDispatchExecution` row(s) and
`AgentWorkAssignmentEvent` `status_changed` history, never a new cached counter."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_coordination import (
    AgentDispatchExecution,
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
)
from app.models.resource_intelligence import AgentResourceTelemetrySample
from app.resource_intelligence.types import MetricEnvelope, ResourceIntelligenceError, unknown_metric

_SOURCE = "agent_resource_telemetry_samples"

# Classification of AgentWorkAssignment.status (see app.models.agent_coordination
# .WorkAssignmentStatus) into the three idle/productive/blocked buckets -- read directly off
# the real status_changed event history, never guessed. Terminal statuses close the timeline
# and contribute to none of the three buckets themselves.
_BLOCKED_STATUSES = frozenset({"blocked", "changes_requested", "waiting_dependency", "waiting_review"})
_IDLE_STATUSES = frozenset({"planned", "ready", "waiting_agent", "reviewing", "ready_for_review"})
_PRODUCTIVE_STATUSES = frozenset({"running"})


def _get_owned_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> AgentWorkAssignment | None:
    return db.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.id == assignment_id, AgentWorkAssignment.owner_id == owner_id)
    ).scalar_one_or_none()


def record_telemetry_sample(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment_id: uuid.UUID,
    attempt_id: uuid.UUID,
    context_used_tokens: int | None = None,
    context_window_tokens: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    tool_calls: int | None = None,
    provenance: dict | None = None,
) -> AgentResourceTelemetrySample:
    """Records ONE point-in-time observation. Validates `assignment_id` belongs to `owner_id`
    BEFORE inserting -- defense in depth alongside RLS, matching this codebase's established
    doctrine (see e.g. `app.agent_coordination.runtime_view.agent_runtime_snapshot()`'s own
    identical reasoning) -- never relying on the session's RLS context alone. Every numeric
    field defaults to `None` and is stored exactly as given: a caller records only what it
    actually knows, this function never fabricates or infers a value for a field the caller
    omitted."""

    assignment = _get_owned_assignment(db, owner_id=owner_id, assignment_id=assignment_id)
    if assignment is None:
        raise ResourceIntelligenceError(f"assignment_id={assignment_id} does not belong to owner_id={owner_id}")

    sample = AgentResourceTelemetrySample(
        owner_id=owner_id,
        assignment_id=assignment_id,
        attempt_id=attempt_id,
        context_used_tokens=context_used_tokens,
        context_window_tokens=context_window_tokens,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        tool_calls=tool_calls,
        provenance=provenance or {},
    )
    db.add(sample)
    db.flush()
    return sample


def list_telemetry_samples(db: Session, *, owner_id: uuid.UUID, attempt_id: uuid.UUID) -> list[AgentResourceTelemetrySample]:
    return list(
        db.execute(
            select(AgentResourceTelemetrySample)
            .where(AgentResourceTelemetrySample.owner_id == owner_id, AgentResourceTelemetrySample.attempt_id == attempt_id)
            .order_by(AgentResourceTelemetrySample.sampled_at)
        ).scalars()
    )


def context_utilization(db: Session, *, owner_id: uuid.UUID, attempt_id: uuid.UUID) -> MetricEnvelope:
    """The LATEST sample's `context_used_tokens / context_window_tokens` as a percent.
    `missing_data=True` if no samples exist, or the latest sample is missing either field, or
    `context_window_tokens` is `0` (a percentage against a zero-sized window is not a real
    number, never silently reported as 0% or 100%)."""

    definition = "latest observed context_used_tokens / context_window_tokens, expressed as a percent"
    samples = list_telemetry_samples(db, owner_id=owner_id, attempt_id=attempt_id)
    if not samples:
        return unknown_metric(unit="percent", definition=definition, source=_SOURCE, method="no telemetry samples recorded for this attempt_id")

    latest = samples[-1]
    if latest.context_used_tokens is None or latest.context_window_tokens is None:
        return unknown_metric(
            unit="percent", definition=definition, source=_SOURCE,
            method=f"latest sample (sampled_at={latest.sampled_at.isoformat()}) is missing context_used_tokens and/or context_window_tokens",
        )
    if latest.context_window_tokens == 0:
        return unknown_metric(
            unit="percent", definition=definition, source=_SOURCE,
            method=f"latest sample (sampled_at={latest.sampled_at.isoformat()}) reports context_window_tokens=0",
        )

    value = 100.0 * latest.context_used_tokens / latest.context_window_tokens

    trend = None
    if len(samples) >= 2:
        prior = samples[-2]
        if prior.context_used_tokens is not None and prior.context_window_tokens not in (None, 0):
            prior_value = 100.0 * prior.context_used_tokens / prior.context_window_tokens
            if value > prior_value:
                trend = "increasing"
            elif value < prior_value:
                trend = "decreasing"
            else:
                trend = "stable"

    return MetricEnvelope(
        value=value,
        unit="percent",
        definition=definition,
        denominator="context_window_tokens",
        time_window=f"point_in_time:{latest.sampled_at.isoformat()}",
        population=f"attempt_id={attempt_id}",
        sample_size=1,
        source=_SOURCE,
        method=f"value = 100 * {latest.context_used_tokens} / {latest.context_window_tokens} (latest sample only)",
        missing_data=False,
        uncertainty="point-in-time snapshot from the latest recorded sample; not an average over the attempt",
        last_updated=latest.sampled_at,
        trend=trend,
    )


def estimated_time_to_context_limit(db: Session, *, owner_id: uuid.UUID, attempt_id: uuid.UUID) -> MetricEnvelope:
    """Linear projection from the most recent two samples' own observed burn rate
    (tokens/elapsed-second) against the remaining headroom to `context_window_tokens`.
    `missing_data=True` with fewer than 2 usable samples, an unknown window size, or a
    non-positive burn rate (context is not growing -- no meaningful "time to limit" to
    report)."""

    definition = "projected wall-clock seconds until context_used_tokens reaches context_window_tokens, linearly extrapolated from the two most recent telemetry samples"
    samples = [s for s in list_telemetry_samples(db, owner_id=owner_id, attempt_id=attempt_id) if s.context_used_tokens is not None]
    if len(samples) < 2:
        return unknown_metric(
            unit="seconds", definition=definition, source=_SOURCE,
            method=f"{len(samples)} sample(s) with a known context_used_tokens; at least 2 are required to compute a burn rate",
        )

    prior, latest = samples[-2], samples[-1]
    if latest.context_window_tokens is None:
        return unknown_metric(
            unit="seconds", definition=definition, source=_SOURCE,
            method=f"latest sample (sampled_at={latest.sampled_at.isoformat()}) is missing context_window_tokens",
        )

    elapsed_seconds = (latest.sampled_at - prior.sampled_at).total_seconds()
    if elapsed_seconds <= 0:
        return unknown_metric(
            unit="seconds", definition=definition, source=_SOURCE,
            method=f"the two most recent samples share sampled_at (or are out of order): prior={prior.sampled_at.isoformat()} latest={latest.sampled_at.isoformat()}",
        )

    token_delta = latest.context_used_tokens - prior.context_used_tokens
    rate = token_delta / elapsed_seconds
    method = (
        f"rate = ({latest.context_used_tokens} - {prior.context_used_tokens}) tokens / {elapsed_seconds:.3f}s "
        f"= {rate:.6f} tokens/s"
    )
    if rate <= 0:
        return unknown_metric(
            unit="seconds", definition=definition, source=_SOURCE,
            method=f"{method}; non-positive burn rate has no meaningful time-to-limit projection",
        )

    remaining_headroom = latest.context_window_tokens - latest.context_used_tokens
    if remaining_headroom <= 0:
        eta_seconds = 0.0
    else:
        eta_seconds = remaining_headroom / rate

    return MetricEnvelope(
        value=eta_seconds,
        unit="seconds",
        definition=definition,
        denominator=None,
        time_window=f"{prior.sampled_at.isoformat()}..{latest.sampled_at.isoformat()}",
        population=f"attempt_id={attempt_id}",
        sample_size=2,
        source=_SOURCE,
        method=f"{method}; remaining_headroom={remaining_headroom} tokens; eta_seconds = remaining_headroom / rate",
        missing_data=False,
        uncertainty="linear extrapolation from the 2 most recent samples only; real burn rate may vary (bursty tool calls, compaction, etc.)",
        last_updated=latest.sampled_at,
        trend="increasing" if rate > 0 else None,
    )


def _dispatch_executions_for_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> list[AgentDispatchExecution]:
    return list(
        db.execute(
            select(AgentDispatchExecution)
            .where(AgentDispatchExecution.owner_id == owner_id, AgentDispatchExecution.assignment_id == assignment_id)
            .order_by(AgentDispatchExecution.started_at)
        ).scalars()
    )


def _status_changed_events(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> list[AgentWorkAssignmentEvent]:
    """Mirrors the query pattern `app.agent_coordination.runtime_view
    ._latest_status_changed_detail_reason()` already establishes for reading this exact event
    history -- ordered ascending here (that function orders descending + limit(1) because it
    only wants the latest; this function needs the FULL timeline)."""

    return list(
        db.execute(
            select(AgentWorkAssignmentEvent)
            .where(
                AgentWorkAssignmentEvent.owner_id == owner_id,
                AgentWorkAssignmentEvent.assignment_id == assignment_id,
                AgentWorkAssignmentEvent.event_type == AgentWorkAssignmentEventType.status_changed,
            )
            .order_by(AgentWorkAssignmentEvent.created_at)
        ).scalars()
    )


def idle_productive_blocked_time(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> dict[str, MetricEnvelope]:
    """Derives (never stores) idle/productive/blocked wall-clock seconds for one assignment.

    Method (grounded in the real event/execution history, never a formula guessed in the
    abstract):

    1. The assignment's own `status_changed` `AgentWorkAssignmentEvent` history
       (`app.agent_coordination.execution_control`/`service.transition_status()` is the only
       thing that ever writes one) is read in order, giving a timeline of
       `(interval_start, interval_end, status)` -- each interval running from one status
       change to the next, and the LAST interval running to `AgentWorkAssignment.completed_at`
       if the assignment reached a terminal status, else to "now" (an in-progress assignment's
       most recent status is still ongoing).
    2. Each interval is classified by its status into one of three buckets: `_BLOCKED_STATUSES`
       (explicit blocker -- `blocked`/`changes_requested`/`waiting_dependency`/
       `waiting_review`), `_IDLE_STATUSES` (nobody actively working --
       `planned`/`ready`/`waiting_agent`/`reviewing`/`ready_for_review`), or `_PRODUCTIVE_
       STATUSES` (`running`). Terminal statuses close the timeline and are not themselves a
       bucket.
    3. For `blocked`/`idle` intervals, the FULL interval duration counts -- the assignment
       state machine itself is the ground truth for "nobody is actively running code" there.
    4. For `running` intervals, only the portion ACTUALLY COVERED by a real
       `AgentDispatchExecution` row's own `[started_at, ended_at-or-last_heartbeat_at-or-
       last_output_at-or-now)` span counts as productive -- a `running`-status interval with no
       corroborating dispatch-execution evidence contributes to none of the three buckets
       (never silently folded into "productive" on the assignment-status label alone).

    `missing_data=True` (for all three keys) only when there is genuinely no status_changed
    history to derive from at all; a bucket whose real derived total is `0` (e.g. an
    assignment that was never blocked) is a genuine computed zero, not a fabricated default.
    """

    definitions = {
        "idle_seconds": "wall-clock seconds this assignment spent in a status with no active reviewer/agent action (planned/ready/waiting_agent/reviewing/ready_for_review), derived from status_changed event history",
        "productive_seconds": "wall-clock seconds covered by a real AgentDispatchExecution attempt's own started_at..ended_at span while the assignment's status was 'running'",
        "blocked_seconds": "wall-clock seconds this assignment spent in an explicit blocked/changes_requested/waiting_dependency/waiting_review status, derived from status_changed event history",
    }

    assignment = _get_owned_assignment(db, owner_id=owner_id, assignment_id=assignment_id)
    if assignment is None:
        return {key: unknown_metric(unit="seconds", definition=defn, source="agent_work_assignments", method="assignment_id not found for this owner_id") for key, defn in definitions.items()}

    events = _status_changed_events(db, owner_id=owner_id, assignment_id=assignment_id)
    if not events:
        return {
            key: unknown_metric(unit="seconds", definition=defn, source="agent_work_assignment_events", method="no status_changed events recorded for this assignment")
            for key, defn in definitions.items()
        }

    now = datetime.utcnow()
    end_bound = assignment.completed_at if (assignment.status in TERMINAL_WORK_ASSIGNMENT_STATUSES and assignment.completed_at is not None) else now

    intervals: list[tuple[datetime, datetime, str]] = []
    for i, event in enumerate(events):
        start = event.created_at
        end = events[i + 1].created_at if i + 1 < len(events) else end_bound
        status = (event.detail or {}).get("to")
        if status and end > start:
            intervals.append((start, end, status))

    executions = _dispatch_executions_for_assignment(db, owner_id=owner_id, assignment_id=assignment_id)

    def _execution_end(execution: AgentDispatchExecution) -> datetime:
        return execution.ended_at or execution.last_heartbeat_at or execution.last_output_at or now

    idle_total = 0.0
    blocked_total = 0.0
    productive_total = 0.0
    for start, end, status in intervals:
        if status in _PRODUCTIVE_STATUSES:
            for execution in executions:
                overlap_start = max(start, execution.started_at)
                overlap_end = min(end, _execution_end(execution))
                if overlap_end > overlap_start:
                    productive_total += (overlap_end - overlap_start).total_seconds()
        elif status in _BLOCKED_STATUSES:
            blocked_total += (end - start).total_seconds()
        elif status in _IDLE_STATUSES:
            idle_total += (end - start).total_seconds()
        # Any other/terminal status contributes to none of the three buckets.

    sample_size = len(events)
    time_window = f"{events[0].created_at.isoformat()}..{end_bound.isoformat()}"
    method_suffix = f"derived from {sample_size} status_changed event(s) and {len(executions)} dispatch execution attempt(s)"

    def _envelope(key: str, value: float) -> MetricEnvelope:
        return MetricEnvelope(
            value=value,
            unit="seconds",
            definition=definitions[key],
            denominator=None,
            time_window=time_window,
            population=f"assignment_id={assignment_id}",
            sample_size=sample_size,
            source="agent_work_assignment_events + agent_dispatch_executions",
            method=method_suffix,
            missing_data=False,
            uncertainty="running-status time with no corroborating AgentDispatchExecution span is excluded from all three buckets, not folded into productive_seconds",
            last_updated=end_bound,
            trend=None,
        )

    return {
        "idle_seconds": _envelope("idle_seconds", idle_total),
        "productive_seconds": _envelope("productive_seconds", productive_total),
        "blocked_seconds": _envelope("blocked_seconds", blocked_total),
    }
