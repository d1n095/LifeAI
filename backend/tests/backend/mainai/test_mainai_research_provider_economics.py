"""MainAI Research -- `app.mainai_research.provider_economics` + `procurement_review` -- proves
ONE EXPENSIVE DAY / ONE GOOD RESULT != ENOUGH, cheap-provider-with-rework loses on total
verified-outcome cost, rare-but-valuable providers are kept as fallback, and an adversarial
challenge on a procurement call remains visible rather than silently overridden.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture these
modules implement."""

from __future__ import annotations

import pytest

from app.mainai_research.council import ReviewVerdict, SpecialistReview
from app.mainai_research.procurement_review import UserHarmReviewFinding, review_procurement_recommendation, review_user_facing_wording
from app.mainai_research.provider_economics import (
    ProviderEconomicsSignal,
    compare_total_verified_outcome_cost,
    recommend_provider_action,
)
from app.mainai_research.types import ProviderRecommendation, SpecialistRole


def test_thin_history_never_recommends_removal():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=1, cost_per_accepted_commit_usd=50.0, rework_rate=0.9, accepted_commit_rate=0.1, quota_remaining_fraction=None)
    assessment = recommend_provider_action(signal)
    assert assessment.recommendation == ProviderRecommendation.TRIAL
    assert assessment.sufficient_history is False


def test_thin_history_with_strategic_value_keeps_as_fallback():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=2, cost_per_accepted_commit_usd=None, rework_rate=None, accepted_commit_rate=None, quota_remaining_fraction=None, strategic_fallback_value=True)
    assessment = recommend_provider_action(signal)
    assert assessment.recommendation == ProviderRecommendation.KEEP_AS_FALLBACK


def test_established_high_rework_recommends_model_change():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=20, cost_per_accepted_commit_usd=5.0, rework_rate=0.6, accepted_commit_rate=0.9, quota_remaining_fraction=0.5)
    assessment = recommend_provider_action(signal)
    assert assessment.recommendation == ProviderRecommendation.CHANGE_DEFAULT_MODEL


def test_established_low_acceptance_recommends_removal_without_strategic_value():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=20, cost_per_accepted_commit_usd=5.0, rework_rate=0.1, accepted_commit_rate=0.2, quota_remaining_fraction=0.5)
    assessment = recommend_provider_action(signal)
    assert assessment.recommendation == ProviderRecommendation.REMOVE


def test_rare_provider_with_unique_capability_kept_despite_low_use():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=20, cost_per_accepted_commit_usd=5.0, rework_rate=0.1, accepted_commit_rate=0.2, quota_remaining_fraction=0.5, unique_capability=True)
    assessment = recommend_provider_action(signal)
    assert assessment.recommendation == ProviderRecommendation.KEEP_AS_FALLBACK


def test_expensive_provider_wins_on_total_verified_outcome_cost():
    assert compare_total_verified_outcome_cost(cheap_provider_cost_per_accepted_commit=20.0, expensive_provider_cost_per_accepted_commit=8.0) == ProviderRecommendation.UPGRADE


def test_cheap_provider_with_low_rework_is_kept():
    assert compare_total_verified_outcome_cost(cheap_provider_cost_per_accepted_commit=3.0, expensive_provider_cost_per_accepted_commit=8.0) == ProviderRecommendation.KEEP


def test_quota_critical_recommends_change_provider():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=20, cost_per_accepted_commit_usd=5.0, rework_rate=0.1, accepted_commit_rate=0.9, quota_remaining_fraction=0.02)
    assessment = recommend_provider_action(signal)
    assert assessment.recommendation == ProviderRecommendation.CHANGE_PROVIDER


def test_adversarial_challenge_on_procurement_call_is_visible_and_does_not_stand_silently():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=20, cost_per_accepted_commit_usd=5.0, rework_rate=0.1, accepted_commit_rate=0.2, quota_remaining_fraction=0.5)
    economics = recommend_provider_action(signal)
    reviews = (
        SpecialistReview(role=SpecialistRole.ANALYST, verdict=ReviewVerdict.SUPPORTS, reasoning="matches observed pattern"),
        SpecialistReview(role=SpecialistRole.ECONOMIST, verdict=ReviewVerdict.SUPPORTS, reasoning="cost data supports removal"),
        SpecialistReview(role=SpecialistRole.ADVERSARIAL_COUNSEL, verdict=ReviewVerdict.DISPUTES, reasoning="sample may be biased toward one hard task type"),
    )
    decision = review_procurement_recommendation(economics_assessment=economics, specialist_reviews=reviews)
    assert decision.challenged is True
    assert decision.final_recommendation_stands is False
    assert decision.authorized is False


def test_unanimous_procurement_recommendation_stands():
    signal = ProviderEconomicsSignal(provider_id="p1", observation_count=20, cost_per_accepted_commit_usd=5.0, rework_rate=0.1, accepted_commit_rate=0.2, quota_remaining_fraction=0.5)
    economics = recommend_provider_action(signal)
    reviews = (
        SpecialistReview(role=SpecialistRole.ANALYST, verdict=ReviewVerdict.SUPPORTS, reasoning="matches observed pattern"),
        SpecialistReview(role=SpecialistRole.ECONOMIST, verdict=ReviewVerdict.SUPPORTS, reasoning="cost data supports removal"),
    )
    decision = review_procurement_recommendation(economics_assessment=economics, specialist_reviews=reviews)
    assert decision.final_recommendation_stands is True


def test_technically_true_but_misleading_wording_is_flagged():
    result = review_user_facing_wording(
        wording="Free forever*", findings=(UserHarmReviewFinding(concern="buried limitation", evidence="asterisk footnote reveals a paid tier after 14 days", severity="high"),),
    )
    assert result.has_high_severity_finding is True
    assert "revise" in result.recommendation.lower()


def test_user_harm_review_requires_real_wording():
    with pytest.raises(ValueError):
        review_user_facing_wording(wording="")
