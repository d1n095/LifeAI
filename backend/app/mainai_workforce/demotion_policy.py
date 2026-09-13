"""Capability Demotion Policy. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

Demotion has no minimum-evidence floor the way promotion does -- a real regression should be
responsive, not gated behind a sample-size requirement (the asymmetry is deliberate: promotion
requires strong evidence to CLIMB; demotion requires only real evidence that something got
worse, matching `mainai_research.falsification`'s own "a real contradiction is never outvoted
by prior confidence" asymmetry).

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

RECENT_FAILURE_RATE_DEMOTION_BAR = 0.25
STALENESS_DAYS_DEMOTION_BAR = 180


@dataclass(frozen=True)
class DemotionAssessment:
    triggered: bool
    reason: str


def assess_demotion_trigger(
    *,
    recent_failure_rate_delta: float,
    architecture_invalidated: bool = False,
    examiner_pass_rate_delta: float = 0.0,
    staleness_days: float = 0.0,
    external_expert_found_new_missed_bug_class: bool = False,
) -> DemotionAssessment:
    """First real trigger wins. A NEW architecture invalidating the old skill demotes
    unconditionally, regardless of how good the historical numbers still look -- old evidence
    describes a system that no longer exists."""

    if architecture_invalidated:
        return DemotionAssessment(True, "new architecture invalidates the old skill's own evidence base")

    if external_expert_found_new_missed_bug_class:
        return DemotionAssessment(True, "external expert repeatedly found a class of bug the local skill misses")

    if recent_failure_rate_delta >= RECENT_FAILURE_RATE_DEMOTION_BAR:
        return DemotionAssessment(True, f"recent failure rate rose by {recent_failure_rate_delta:.2f} (>= {RECENT_FAILURE_RATE_DEMOTION_BAR})")

    if examiner_pass_rate_delta <= -RECENT_FAILURE_RATE_DEMOTION_BAR:
        return DemotionAssessment(True, f"examiner pass rate fell by {abs(examiner_pass_rate_delta):.2f}")

    if staleness_days >= STALENESS_DAYS_DEMOTION_BAR:
        return DemotionAssessment(True, f"no verified observation in {staleness_days:.0f} day(s) (>= {STALENESS_DAYS_DEMOTION_BAR}) -- currentness lapsed")

    return DemotionAssessment(False, "no demotion trigger met")
