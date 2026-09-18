"""Composition seams for `app.mainai_workforce`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

Real composition happens directly inside each module that needs it:
`situational_snapshot.py` calls real `app.agent_coordination.runtime_view`;
`workforce_scheduler.py` calls real `app.mainai_cognitive_ops`; `capability_learning_loop.py`
calls this package's own real `mastery_ledger.py`. This file discloses the one seam that is
NOT yet real: `app.resource_intelligence.scheduler.next_best_resource_allocation()` answers a
DIFFERENT, complementary question (in-flight session-lifecycle triage) and is intentionally
NOT called from inside `workforce_scheduler.recommend_for_task()` -- a caller wanting BOTH
signals together composes them at the call site, since merging them would blur two genuinely
different recommendations into one, matching this whole program's own "compose, don't
conflate" discipline."""

from __future__ import annotations

from typing import Protocol


class ResourceIntelligenceInFlightAdapter(Protocol):
    """Would supply `next_best_resource_allocation()`'s own ranked in-flight triage alongside
    this package's not-yet-assigned wait/assign recommendations for a single combined founder
    view. Not composed here by design -- see this module's own docstring."""

    def next_best_resource_allocation(self) -> list: ...
