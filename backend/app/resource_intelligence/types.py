"""Shared vocabulary for `app.resource_intelligence` -- see
docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md for the full architecture
decision this package implements.

METRIC != TRUTH. UNKNOWN STAYS UNKNOWN. RESOURCE_OPTIMIZATION != AUTHORITY.

Every metric-returning function in this package returns a `MetricEnvelope`, never a bare
number -- enforced by the type signature, not by convention alone. Every optional telemetry
field is `None` when not observed, never defaulted to zero or silently estimated; `unknown_
metric()` is the ONE canonical constructor every function in this package uses when it
genuinely has no data, so "what does UNKNOWN look like" is answered in exactly one place.

`ResourceActionRecommendation` mirrors `app.mainai_executive.judgment.JudgmentDecision`'s own
proven shape exactly (`action`, `reason`, `signals: dict`, `authorized: bool = False`) --
`authorized` is always `False` here too: this package recommends, it never itself compacts,
resets, hands off, or changes a real session's model/provider (see `RESOURCE_OPTIMIZATION !=
AUTHORITY` in the reconciliation doc's §1.6). No decision engine ships in this round (Part 1);
this enum/dataclass pair is the vocabulary Part 2's `decision.py` will return."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ResourceIntelligenceError(ValueError):
    """Raised by this package's own functions on a structural/ownership violation -- e.g. an
    `assignment_id` that does not belong to the calling `owner_id`. Never raised for "no data
    was observed" -- that is always a `MetricEnvelope` with `missing_data=True`, never an
    exception, so a caller can always render a metric-shaped answer."""


class ContextLifecycleAction(str, enum.Enum):
    """The full action vocabulary a future `propose_resource_action()` (Part 2) recommends
    from -- never itself executed by this package. See reconciliation doc §1.1."""

    CONTINUE_CURRENT_SESSION = "CONTINUE_CURRENT_SESSION"
    COMPACT = "COMPACT"
    CHECKPOINT = "CHECKPOINT"
    RESET_SESSION = "RESET_SESSION"
    HANDOFF = "HANDOFF"
    SPLIT_JOB = "SPLIT_JOB"
    MOVE_SUBTASK = "MOVE_SUBTASK"
    CHANGE_MODEL = "CHANGE_MODEL"
    CHANGE_PROVIDER = "CHANGE_PROVIDER"
    DEFER = "DEFER"
    KEEP_CURRENT_AGENT = "KEEP_CURRENT_AGENT"


@dataclass(frozen=True)
class ResourceActionRecommendation:
    """A pure, advisory recommendation -- never a grant of authority to act. Mirrors
    `app.mainai_executive.judgment.JudgmentDecision`'s exact shape; see this module's own
    docstring."""

    action: ContextLifecycleAction
    reason: str
    signals: dict[str, Any]
    authorized: bool = False


@dataclass(frozen=True)
class MetricEnvelope:
    """The metrics-quality contract, structural, not conventional: METRIC != TRUTH. Every
    field below is always present (never omitted) -- `value` is the only one allowed to be
    `None` (when `missing_data=True`), every OTHER field must still be honestly filled in even
    when the value itself is unknown, so a caller can always see WHY a metric is missing, not
    just THAT it is."""

    value: float | int | None
    unit: str
    definition: str
    denominator: str | None
    time_window: str | None
    population: str | None
    sample_size: int | None
    source: str
    method: str
    missing_data: bool
    uncertainty: str | None
    last_updated: datetime
    trend: str | None


def unknown_metric(*, unit: str, definition: str, source: str, method: str | None = None, uncertainty: str | None = None) -> MetricEnvelope:
    """The canonical "UNKNOWN stays UNKNOWN" constructor -- `value=None`, `missing_data=True`,
    every other field honestly filled in from what the caller actually knows (never a guess).
    Every function in this package that genuinely has no data returns THIS, rather than each
    one inventing its own null-handling."""

    return MetricEnvelope(
        value=None,
        unit=unit,
        definition=definition,
        denominator=None,
        time_window=None,
        population=None,
        sample_size=None,
        source=source,
        method=method or "no observation available",
        missing_data=True,
        uncertainty=uncertainty,
        last_updated=datetime.utcnow(),
        trend=None,
    )
