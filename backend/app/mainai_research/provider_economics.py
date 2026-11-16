"""Provider Economics & Procurement -- advisory only. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

PROVIDER ADVISORY != PURCHASE AUTHORITY. SUBSCRIPTION CHANGE REQUIRES FOUNDER AUTHORIZATION.
ONE EXPENSIVE DAY IS NOT ENOUGH. ONE GOOD RESULT IS NOT ENOUGH -- `recommend_provider_action()`
structurally requires a minimum observation history before recommending anything beyond TRIAL.

Built on real `app.resource_intelligence` signals (`cost_per_accepted_commit`,
`provider_quota_remaining`, `agent_efficiency_profile`) -- never a second cost/quota ledger."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_research.types import ProviderRecommendation

# Documented, hand-picked starting points -- ONE EXPENSIVE DAY != ENOUGH HISTORY.
MIN_OBSERVATIONS_FOR_STRONG_RECOMMENDATION = 10
HIGH_REWORK_RATE_BAR = 0.4
LOW_ACCEPTED_RATE_BAR = 0.5
QUOTA_CRITICAL_FRACTION = 0.1


@dataclass(frozen=True)
class ProviderEconomicsSignal:
    """Plain, caller-supplied real signals from `resource_intelligence` -- this module never
    computes these itself, only reasons over them (matching every other advisory module in this
    program's own "caller supplies the real signal" convention)."""

    provider_id: str
    observation_count: int
    cost_per_accepted_commit_usd: float | None
    rework_rate: float | None
    accepted_commit_rate: float | None
    quota_remaining_fraction: float | None
    unique_capability: bool = False  # this provider does something no other available provider does
    strategic_fallback_value: bool = False


@dataclass(frozen=True)
class ProviderEconomicsAssessment:
    provider_id: str
    recommendation: ProviderRecommendation
    reason: str
    sufficient_history: bool
    authorized: bool = False


def recommend_provider_action(signal: ProviderEconomicsSignal) -> ProviderEconomicsAssessment:
    """Pure. ONE EXPENSIVE DAY / ONE GOOD RESULT IS NOT ENOUGH: below
    `MIN_OBSERVATIONS_FOR_STRONG_RECOMMENDATION`, the strongest allowed recommendation is TRIAL
    (gather more history) or KEEP_AS_FALLBACK (if it has real, disclosed strategic value) --
    never REMOVE/UPGRADE/CHANGE_PROVIDER on thin history alone."""

    sufficient = signal.observation_count >= MIN_OBSERVATIONS_FOR_STRONG_RECOMMENDATION

    if not sufficient:
        if signal.strategic_fallback_value or signal.unique_capability:
            return ProviderEconomicsAssessment(
                provider_id=signal.provider_id, recommendation=ProviderRecommendation.KEEP_AS_FALLBACK,
                reason=f"only {signal.observation_count} observation(s) (< {MIN_OBSERVATIONS_FOR_STRONG_RECOMMENDATION}) -- "
                       "insufficient history for a strong recommendation, but real disclosed strategic/unique value keeps it as a fallback",
                sufficient_history=False,
            )
        return ProviderEconomicsAssessment(
            provider_id=signal.provider_id, recommendation=ProviderRecommendation.TRIAL,
            reason=f"only {signal.observation_count} observation(s) (< {MIN_OBSERVATIONS_FOR_STRONG_RECOMMENDATION}) -- "
                   "ONE EXPENSIVE DAY / ONE GOOD RESULT IS NOT ENOUGH; gather more history before a stronger call",
            sufficient_history=False,
        )

    if signal.quota_remaining_fraction is not None and signal.quota_remaining_fraction <= QUOTA_CRITICAL_FRACTION:
        return ProviderEconomicsAssessment(
            provider_id=signal.provider_id, recommendation=ProviderRecommendation.CHANGE_PROVIDER,
            reason=f"quota_remaining_fraction={signal.quota_remaining_fraction:.2f} <= {QUOTA_CRITICAL_FRACTION} with {signal.observation_count} observations",
            sufficient_history=True,
        )

    if signal.rework_rate is not None and signal.rework_rate >= HIGH_REWORK_RATE_BAR:
        return ProviderEconomicsAssessment(
            provider_id=signal.provider_id, recommendation=ProviderRecommendation.CHANGE_DEFAULT_MODEL,
            reason=f"established rework_rate={signal.rework_rate:.2f} (>= {HIGH_REWORK_RATE_BAR}) over {signal.observation_count} observations -- "
                   "cheap-per-token does not mean cheap overall once rework cost is counted",
            sufficient_history=True,
        )

    if signal.accepted_commit_rate is not None and signal.accepted_commit_rate < LOW_ACCEPTED_RATE_BAR:
        recommendation = ProviderRecommendation.KEEP_AS_FALLBACK if (signal.strategic_fallback_value or signal.unique_capability) else ProviderRecommendation.REMOVE
        return ProviderEconomicsAssessment(
            provider_id=signal.provider_id, recommendation=recommendation,
            reason=f"established accepted_commit_rate={signal.accepted_commit_rate:.2f} (< {LOW_ACCEPTED_RATE_BAR}) over {signal.observation_count} observations",
            sufficient_history=True,
        )

    return ProviderEconomicsAssessment(
        provider_id=signal.provider_id, recommendation=ProviderRecommendation.KEEP,
        reason=f"no established signal over {signal.observation_count} observations biases away from keeping this provider",
        sufficient_history=True,
    )


def compare_total_verified_outcome_cost(
    *, cheap_provider_cost_per_accepted_commit: float, expensive_provider_cost_per_accepted_commit: float,
) -> ProviderRecommendation:
    """CHEAPEST MODEL != CHEAPEST OUTCOME (reused invariant from `resource_intelligence.
    decision.py` -- this is that same comparison, applied at the procurement-recommendation
    layer). Compares REAL cost-per-ACCEPTED-commit (already amortizes rework into the cheap
    provider's own number, per `resource_intelligence.cost_bridge`'s own doctrine), never a raw
    per-token price."""

    if expensive_provider_cost_per_accepted_commit < cheap_provider_cost_per_accepted_commit:
        return ProviderRecommendation.UPGRADE
    return ProviderRecommendation.KEEP
