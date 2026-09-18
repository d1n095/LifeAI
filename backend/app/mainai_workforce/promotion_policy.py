"""Capability Promotion Policy. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

ONE LOCAL SUCCESS != MASTERY. Promotion requires a minimum sample count AND task diversity AND
a real independent-examiner pass rate AND recency -- never a single success, regardless of how
clean it looked.

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

MIN_OBSERVATIONS_FOR_PROMOTION = 8
MIN_TASK_DIVERSITY_FOR_PROMOTION = 3
MIN_EXAMINER_PASS_RATE_FOR_PROMOTION = 0.85
MAX_RECENCY_DAYS_FOR_PROMOTION = 90
MAX_REGRESSION_EVENTS_FOR_PROMOTION = 0


@dataclass(frozen=True)
class PromotionAssessment:
    eligible: bool
    reason: str


def assess_promotion_eligibility(
    *,
    observation_count: int,
    distinct_task_diversity: int,
    examiner_pass_rate: float | None,
    recency_days: float,
    regression_events: int = 0,
) -> PromotionAssessment:
    """First failing check wins -- ONE LOCAL SUCCESS != MASTERY is enforced structurally by
    `MIN_OBSERVATIONS_FOR_PROMOTION`/`MIN_TASK_DIVERSITY_FOR_PROMOTION`: a single success can
    satisfy neither on its own."""

    if observation_count < MIN_OBSERVATIONS_FOR_PROMOTION:
        return PromotionAssessment(False, f"only {observation_count} observation(s) (< {MIN_OBSERVATIONS_FOR_PROMOTION}) -- ONE LOCAL SUCCESS != MASTERY")

    if distinct_task_diversity < MIN_TASK_DIVERSITY_FOR_PROMOTION:
        return PromotionAssessment(False, f"only {distinct_task_diversity} distinct task variant(s) (< {MIN_TASK_DIVERSITY_FOR_PROMOTION}) -- repeated identical tasks do not prove generalization")

    if examiner_pass_rate is None:
        return PromotionAssessment(False, "no independent-examiner pass rate recorded -- cannot promote on unexamined local success alone")

    if examiner_pass_rate < MIN_EXAMINER_PASS_RATE_FOR_PROMOTION:
        return PromotionAssessment(False, f"examiner_pass_rate={examiner_pass_rate:.2f} (< {MIN_EXAMINER_PASS_RATE_FOR_PROMOTION})")

    if recency_days > MAX_RECENCY_DAYS_FOR_PROMOTION:
        return PromotionAssessment(False, f"most recent verified observation is {recency_days:.0f} day(s) old (> {MAX_RECENCY_DAYS_FOR_PROMOTION}) -- stale evidence")

    if regression_events > MAX_REGRESSION_EVENTS_FOR_PROMOTION:
        return PromotionAssessment(False, f"{regression_events} regression event(s) recorded since the last promotion -- not eligible until resolved")

    return PromotionAssessment(
        True,
        f"{observation_count} observations across {distinct_task_diversity} distinct task variants, "
        f"examiner_pass_rate={examiner_pass_rate:.2f}, recency={recency_days:.0f}d, {regression_events} regression(s)",
    )
