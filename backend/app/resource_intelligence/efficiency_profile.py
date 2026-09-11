"""MainAI Resource Intelligence -- Part 2: the genuinely new statistical layer
`app.capability_reality` never had. See
docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §1.5 for the architecture
decision this module implements.

`agent_efficiency_profile()` computes a real, N-observation, recency-weighted running profile
per `(agent_id, task_type)` FROM real underlying data only -- Part 1's own
`app.resource_intelligence.telemetry`/`cost_bridge` functions, plus `AgentWorkAssignment`'s own
terminal outcome statuses and `AgentWorkAssignmentEvent`'s own append-only `status_changed`
history (`app.agent_coordination`). Nothing here is stored, cached, or duplicated -- every call
recomputes from those real, already-durable sources, matching this codebase's own "derive,
never duplicate" convention (`AgentRuntimeView.heartbeat_at`, `telemetry.
idle_productive_blocked_time()`).

There is no dedicated `task_type` column anywhere on `AgentWorkAssignment` -- the closest real
"what kind of task was this" concept the schema actually has is `AgentWorkAssignment.role`
(`WorkAssignmentRole`: builder/reviewer/tester/challenger/researcher/synthesizer). This module's
own `task_type` parameter is therefore a filter on that REAL column, not an invented one; an
unrecognized value raises `ResourceIntelligenceError` (a structural/ownership-shaped violation,
matching `app.resource_intelligence.types.ResourceIntelligenceError`'s own documented use),
rather than silently matching nothing.

ONE_RUN != LONG_TERM_PROFILE, enforced structurally, not just documented: `MIN_SAMPLE_SIZE_FOR_
ESTABLISHED` (see its own docstring for the exact number and why) gates every returned
`MetricEnvelope` below `provisional` vs `established` -- below the threshold, every envelope this
function returns carries an explicit `PROVISIONAL:` note appended to its own `uncertainty` field
(see `_with_provisional_note()`), never silently presented as a stable trait.

Recency weighting: exponential decay by each terminal assignment's own `completed_at` age,
`weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)` -- the SAME formula shape
`app.mainai_executive.retrieval.rank_by_strength()`'s own `_recency_score()` already uses for an
unrelated purpose (active-context ranking); this module does not import that function (a
different domain, a different half-life), it reuses the MATH shape only, per this round's own
brief. A long-past bad run is never discarded -- it still contributes, just down-weighted -- so a
single anomalous session cannot permanently flip an otherwise-good track record, and a single
anomalous GOOD session cannot instantly erase a real bad track record either.

PROFILE != AUTHORITY: this module is read-only end to end (a single `Session` is used only for
SELECT queries; nothing here ever calls `db.add`/`db.commit`/`db.flush`) and never itself gates
or blocks a real assignment from being created -- it is advisory data for `decision.py` (and
eventually a human) to weigh, exactly like `app.capability_reality`'s own status never itself
blocks anything without a separate, explicit authority check elsewhere."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_coordination import (
    TERMINAL_WORK_ASSIGNMENT_STATUSES,
    AgentDispatchExecution,
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    WorkAssignmentRole,
    WorkAssignmentStatus,
)
from app.resource_intelligence.cost_bridge import cost_per_accepted_commit
from app.resource_intelligence.telemetry import list_telemetry_samples
from app.resource_intelligence.types import MetricEnvelope, ResourceIntelligenceError, unknown_metric

_SOURCE_ASSIGNMENTS = "agent_work_assignments (terminal statuses)"
_SOURCE_EVENTS = "agent_work_assignment_events (status_changed history)"
_SOURCE_TELEMETRY = "agent_resource_telemetry_samples (via agent_dispatch_executions.attempt_id)"

# Small, defensible, documented minimum -- NOT arbitrary: below 5 independent terminal outcomes,
# a single anomalous run can swing an observed rate by >= 20 percentage points (1/5), which is
# large enough to flip a profile from "looks fine" to "looks bad" (or vice versa) on pure noise.
# 5 is the same order of magnitude as this codebase's other hand-picked conservative-default
# thresholds (e.g. `app.mainai_executive.judgment`'s own 0.5/0.8 confidence bars, `wip_awareness
# .default_wip_limit()`) -- a first, weak read is allowed at N=1, but nothing here is presented
# as a stable TRAIT of the agent until there is enough independent evidence that one bad (or
# good) run can no longer single-handedly explain the whole picture.
MIN_SAMPLE_SIZE_FOR_ESTABLISHED = 5

# Exponential half-life for recency weighting of terminal assignments, in days. Longer than
# `retrieval.rank_by_strength()`'s own 14-day default (that function ranks moment-to-moment
# active-context relevance; an agent's own efficiency TRACK RECORD is meant to persist longer
# than that) but still short enough that a run from several months ago contributes only a small
# fraction of a fresh one's weight -- a deliberate, documented choice, not a measured constant
# (no historical data exists yet to fit one against).
RECENCY_HALF_LIFE_DAYS = 30.0

_ACCEPTED_STATUSES = frozenset({WorkAssignmentStatus.completed, WorkAssignmentStatus.verified})

PROFILE_METRIC_KEYS = ("accepted_commit_rate", "rework_rate", "cost_per_accepted_commit", "context_efficiency")


def is_provisional(sample_size: int | None) -> bool:
    """The one, canonical gate every caller (this module's own envelopes, `decision.py`, and
    tests) uses to ask "is this profile still provisional." `None` (unknown sample size) is
    always treated as provisional -- an honest unknown is never treated as "established" by
    omission."""

    return sample_size is None or sample_size < MIN_SAMPLE_SIZE_FOR_ESTABLISHED


def _provisional_note(sample_size: int) -> str:
    return (
        f"PROVISIONAL: this profile is derived from only {sample_size} terminal assignment(s), below "
        f"the minimum of {MIN_SAMPLE_SIZE_FOR_ESTABLISHED} required before it is treated as an "
        "established trait -- ONE_RUN != LONG_TERM_PROFILE; a single anomalous run still dominates "
        "this reading, do not treat it as a stable characterization of this agent."
    )


def _with_provisional_note(envelope: MetricEnvelope, *, sample_size: int) -> MetricEnvelope:
    if not is_provisional(sample_size):
        return envelope
    note = _provisional_note(sample_size)
    combined = f"{envelope.uncertainty}; {note}" if envelope.uncertainty else note
    return replace(envelope, uncertainty=combined)


def _recency_weight(completed_at: datetime, *, now: datetime, half_life_days: float = RECENCY_HALF_LIFE_DAYS) -> float:
    if half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - completed_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def _resolve_role_filter(task_type: str | None) -> WorkAssignmentRole | None:
    if task_type is None:
        return None
    try:
        return WorkAssignmentRole(task_type)
    except ValueError as exc:
        raise ResourceIntelligenceError(
            f"task_type={task_type!r} is not a real WorkAssignmentRole value "
            f"(valid: {[r.value for r in WorkAssignmentRole]}); this module filters on "
            "AgentWorkAssignment.role, the closest real 'kind of task' column that exists"
        ) from exc


def _terminal_assignments(
    db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID, role: WorkAssignmentRole | None
) -> list[AgentWorkAssignment]:
    terminal_values = tuple(s.value for s in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    query = select(AgentWorkAssignment).where(
        AgentWorkAssignment.owner_id == owner_id,
        AgentWorkAssignment.agent_id == agent_id,
        AgentWorkAssignment.status.in_(terminal_values),
    )
    if role is not None:
        query = query.where(AgentWorkAssignment.role == role)
    return list(db.execute(query.order_by(AgentWorkAssignment.completed_at)).scalars())


def _reworked_assignment_ids(db: Session, *, owner_id: uuid.UUID, assignment_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Assignment ids whose own `status_changed` event history contains at least one transition
    INTO `changes_requested` -- the real, defined rework signal, derived from
    `AgentWorkAssignmentEvent.detail["to"]` (the exact field `app.agent_coordination.service.
    transition_status()` writes on every transition), never an invented proxy."""

    if not assignment_ids:
        return set()
    events = list(
        db.execute(
            select(AgentWorkAssignmentEvent).where(
                AgentWorkAssignmentEvent.owner_id == owner_id,
                AgentWorkAssignmentEvent.assignment_id.in_(assignment_ids),
                AgentWorkAssignmentEvent.event_type == AgentWorkAssignmentEventType.status_changed,
            )
        ).scalars()
    )
    target = WorkAssignmentStatus.changes_requested.value
    return {e.assignment_id for e in events if (e.detail or {}).get("to") == target}


def _attempt_ids_for_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(AgentDispatchExecution.attempt_id).where(
                AgentDispatchExecution.owner_id == owner_id,
                AgentDispatchExecution.assignment_id == assignment_id,
            )
        ).scalars()
    )


def _tokens_consumed_for_assignment(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> int | None:
    """Sum of `input_tokens + output_tokens` across every telemetry sample recorded for every
    `AgentDispatchExecution` attempt of this assignment -- `None` (not `0`) when zero samples
    carry any token observation at all, so a genuinely unobserved assignment never masquerades
    as a zero-token one."""

    attempt_ids = _attempt_ids_for_assignment(db, owner_id=owner_id, assignment_id=assignment_id)
    total = 0
    observed_any = False
    for attempt_id in attempt_ids:
        for sample in list_telemetry_samples(db, owner_id=owner_id, attempt_id=attempt_id):
            if sample.input_tokens is not None:
                total += sample.input_tokens
                observed_any = True
            if sample.output_tokens is not None:
                total += sample.output_tokens
                observed_any = True
    return total if observed_any else None


def agent_efficiency_profile(
    db: Session,
    *,
    owner_id: uuid.UUID,
    agent_id: uuid.UUID,
    task_type: str | None = None,
    now: datetime | None = None,
) -> dict[str, MetricEnvelope]:
    """Returns exactly the four keys in `PROFILE_METRIC_KEYS`:

    - `accepted_commit_rate`: recency-weighted fraction of this agent's terminal
      (completed/verified/failed/cancelled/superseded) assignments that reached
      completed/verified.
    - `rework_rate`: recency-weighted fraction of the SAME terminal-assignment population whose
      `status_changed` history shows at least one transition into `changes_requested` before
      reaching a terminal state.
    - `cost_per_accepted_commit`: `cost_bridge.cost_per_accepted_commit()`'s own envelope,
      returned VERBATIM (never reimplemented) -- note this one is always agent-wide, not
      `task_type`-scoped, because that Part 1 function has no `task_type`/`role` parameter of
      its own and this round is forbidden from modifying it; the returned envelope's own
      `uncertainty` records this when `task_type` was requested.
    - `context_efficiency`: recency-weighted average of `input_tokens + output_tokens` summed
      per ACCEPTED (completed/verified) terminal assignment, over every telemetry sample
      recorded for that assignment's own dispatch attempt(s). Assignments with zero telemetry
      samples contribute no data point (excluded, never counted as zero tokens).

    Every recency weight uses the SAME exponential-decay formula
    (`weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)`), keyed off each assignment's own
    `completed_at` -- unconditionally set by `transition_status()` on every terminal transition,
    so a missing `completed_at` on a genuinely terminal row would itself be a data anomaly; such
    a row is excluded from the weighted computations (contributes zero weight) rather than
    crashing.

    `sample_size` on `accepted_commit_rate`/`rework_rate` is the real terminal-assignment count
    used to gate `is_provisional()` -- see this module's own docstring for the exact threshold
    and why. When that overall population is provisional, EVERY returned envelope (including
    the two whose own internal `sample_size` may differ, e.g. `cost_per_accepted_commit`'s own
    completion count or `context_efficiency`'s own count of assignments with telemetry) carries
    the `PROVISIONAL:` note -- the profile as a whole is what is provisional, not just one metric
    within it."""

    now = now or datetime.utcnow()
    role = _resolve_role_filter(task_type)
    population = f"agent_id={agent_id}" + (f", role(task_type)={task_type}" if task_type is not None else "")

    accepted_def = (
        "recency-weighted fraction of this agent's terminal assignments "
        "(completed/verified/failed/cancelled/superseded) that reached completed/verified; each "
        f"assignment weighted by 0.5**(age_days_since_completed_at / {RECENCY_HALF_LIFE_DAYS:.0f})"
    )
    rework_def = (
        "recency-weighted fraction of the SAME terminal-assignment population whose "
        "status_changed event history contains at least one transition into 'changes_requested' "
        "before reaching a terminal state; same recency weighting as accepted_commit_rate"
    )
    context_def = (
        "recency-weighted average of (input_tokens + output_tokens) summed across every "
        "agent_resource_telemetry_samples row recorded for each ACCEPTED (completed/verified) "
        "terminal assignment's own agent_dispatch_executions attempt(s); assignments with zero "
        "recorded telemetry samples contribute no data point, never a fabricated zero"
    )

    assignments = _terminal_assignments(db, owner_id=owner_id, agent_id=agent_id, role=role)
    sample_size = len(assignments)

    if not assignments:
        empty = {
            "accepted_commit_rate": unknown_metric(
                unit="fraction", definition=accepted_def, source=_SOURCE_ASSIGNMENTS,
                method=f"zero terminal assignments found for {population}",
            ),
            "rework_rate": unknown_metric(
                unit="fraction", definition=rework_def, source=_SOURCE_EVENTS,
                method=f"zero terminal assignments found for {population}",
            ),
            "cost_per_accepted_commit": cost_per_accepted_commit(db, owner_id=owner_id, agent_id=agent_id),
            "context_efficiency": unknown_metric(
                unit="tokens_per_completed_assignment", definition=context_def, source=_SOURCE_TELEMETRY,
                method=f"zero terminal assignments found for {population}",
            ),
        }
        return {key: _with_provisional_note(env, sample_size=0) for key, env in empty.items()}

    weights: dict[uuid.UUID, float] = {}
    for a in assignments:
        weights[a.id] = _recency_weight(a.completed_at, now=now) if a.completed_at is not None else 0.0
    total_weight = sum(weights.values())

    time_window = (
        f"{min(a.completed_at for a in assignments if a.completed_at is not None).isoformat()}.."
        f"{max(a.completed_at for a in assignments if a.completed_at is not None).isoformat()}"
        if any(a.completed_at is not None for a in assignments)
        else None
    )
    last_updated = max((a.completed_at for a in assignments if a.completed_at is not None), default=now)

    # -- accepted_commit_rate --
    if total_weight <= 0:
        accepted_envelope = unknown_metric(
            unit="fraction", definition=accepted_def, source=_SOURCE_ASSIGNMENTS,
            method=f"every one of {sample_size} terminal assignment(s) for {population} is missing completed_at; no recency weight could be computed",
        )
    else:
        accepted_weight = sum(weights[a.id] for a in assignments if a.status in _ACCEPTED_STATUSES)
        value = accepted_weight / total_weight
        accepted_envelope = MetricEnvelope(
            value=value, unit="fraction", definition=accepted_def, denominator="recency-weighted total terminal assignments",
            time_window=time_window, population=population, sample_size=sample_size, source=_SOURCE_ASSIGNMENTS,
            method=f"weighted_accepted={accepted_weight:.4f} / weighted_total={total_weight:.4f} over {sample_size} terminal assignment(s), half_life={RECENCY_HALF_LIFE_DAYS:.0f}d",
            missing_data=False, uncertainty=None, last_updated=last_updated, trend=None,
        )

    # -- rework_rate --
    reworked_ids = _reworked_assignment_ids(db, owner_id=owner_id, assignment_ids=[a.id for a in assignments])
    if total_weight <= 0:
        rework_envelope = unknown_metric(
            unit="fraction", definition=rework_def, source=_SOURCE_EVENTS,
            method=f"every one of {sample_size} terminal assignment(s) for {population} is missing completed_at; no recency weight could be computed",
        )
    else:
        reworked_weight = sum(weights[a.id] for a in assignments if a.id in reworked_ids)
        value = reworked_weight / total_weight
        rework_envelope = MetricEnvelope(
            value=value, unit="fraction", definition=rework_def, denominator="recency-weighted total terminal assignments",
            time_window=time_window, population=population, sample_size=sample_size, source=_SOURCE_EVENTS,
            method=f"weighted_reworked={reworked_weight:.4f} / weighted_total={total_weight:.4f} over {sample_size} terminal assignment(s) ({len(reworked_ids)} reworked), half_life={RECENCY_HALF_LIFE_DAYS:.0f}d",
            missing_data=False, uncertainty=None, last_updated=last_updated, trend=None,
        )

    # -- cost_per_accepted_commit -- returned verbatim, never reimplemented.
    cost_envelope = cost_per_accepted_commit(db, owner_id=owner_id, agent_id=agent_id)
    if task_type is not None and not cost_envelope.missing_data:
        note = f"agent-wide, not scoped to task_type={task_type!r}: cost_bridge.cost_per_accepted_commit() has no role/task_type parameter"
        cost_envelope = replace(
            cost_envelope, uncertainty=f"{cost_envelope.uncertainty}; {note}" if cost_envelope.uncertainty else note
        )

    # -- context_efficiency --
    accepted_assignments = [a for a in assignments if a.status in _ACCEPTED_STATUSES]
    token_points: dict[uuid.UUID, int] = {}
    for a in accepted_assignments:
        tokens = _tokens_consumed_for_assignment(db, owner_id=owner_id, assignment_id=a.id)
        if tokens is not None:
            token_points[a.id] = tokens

    if not token_points:
        context_envelope = unknown_metric(
            unit="tokens_per_completed_assignment", definition=context_def, source=_SOURCE_TELEMETRY,
            method=f"none of {len(accepted_assignments)} accepted (completed/verified) assignment(s) for {population} has any telemetry sample recorded",
        )
    else:
        context_weight_total = sum(weights.get(aid, 0.0) for aid in token_points)
        if context_weight_total <= 0:
            context_envelope = unknown_metric(
                unit="tokens_per_completed_assignment", definition=context_def, source=_SOURCE_TELEMETRY,
                method=f"every accepted assignment with telemetry for {population} is missing completed_at; no recency weight could be computed",
            )
        else:
            weighted_tokens = sum(weights.get(aid, 0.0) * tokens for aid, tokens in token_points.items())
            value = weighted_tokens / context_weight_total
            context_envelope = MetricEnvelope(
                value=value, unit="tokens_per_completed_assignment", definition=context_def,
                denominator="recency-weighted count of accepted assignments with >=1 telemetry sample",
                time_window=time_window, population=population, sample_size=len(token_points), source=_SOURCE_TELEMETRY,
                method=f"weighted_tokens={weighted_tokens:.2f} / weighted_count={context_weight_total:.4f} over {len(token_points)} accepted assignment(s) with telemetry, half_life={RECENCY_HALF_LIFE_DAYS:.0f}d",
                missing_data=False,
                uncertainty=f"{len(accepted_assignments) - len(token_points)} accepted assignment(s) had zero telemetry samples and were excluded (not counted as zero)" if len(accepted_assignments) > len(token_points) else None,
                last_updated=last_updated, trend=None,
            )

    profile = {
        "accepted_commit_rate": accepted_envelope,
        "rework_rate": rework_envelope,
        "cost_per_accepted_commit": cost_envelope,
        "context_efficiency": context_envelope,
    }
    return {key: _with_provisional_note(env, sample_size=sample_size) for key, env in profile.items()}
