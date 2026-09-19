"""MainAI Research -- `app.mainai_research.council` + `statistics_integrity` -- proves SPECIALIST
AGREEMENT != TRUTH / disagreement stays visible, and LARGE PERCENTAGE != LARGE ABSOLUTE EFFECT /
REPEATED SOURCE != INDEPENDENT EVIDENCE / METRIC IMPROVEMENT != SYSTEM IMPROVEMENT.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture these
modules implement."""

from __future__ import annotations

from app.mainai_research.council import ReviewVerdict, SpecialistReview, adversarial_critique, synthesize_council_review
from app.mainai_research.statistics_integrity import (
    RelativeRiskClaim,
    assess_metric_vs_system_quality,
    assess_relative_risk_claim,
    assess_source_collapse,
)
from app.mainai_research.types import SpecialistRole
from app.mainai_vision.evidence import EvidenceState, RawEvidence


def test_unanimous_council_is_labeled_unanimous():
    reviews = (
        SpecialistReview(role=SpecialistRole.ANALYST, verdict=ReviewVerdict.SUPPORTS, reasoning="pattern consistent"),
        SpecialistReview(role=SpecialistRole.ECONOMIST, verdict=ReviewVerdict.SUPPORTS, reasoning="incentives align"),
    )
    synthesis = synthesize_council_review(reviews)
    assert synthesis.unanimous is True
    assert synthesis.dissenting_roles == ()


def test_disagreement_stays_visible_never_averaged_away():
    reviews = (
        SpecialistReview(role=SpecialistRole.ANALYST, verdict=ReviewVerdict.SUPPORTS, reasoning="pattern consistent"),
        SpecialistReview(role=SpecialistRole.ADVERSARIAL_COUNSEL, verdict=ReviewVerdict.DISPUTES, reasoning="alternative explanation fits better"),
    )
    synthesis = synthesize_council_review(reviews)
    assert synthesis.unanimous is False
    assert SpecialistRole.ADVERSARIAL_COUNSEL in synthesis.dissenting_roles
    assert "disagreement" in synthesis.integrated_summary.lower()
    assert synthesis.authorized is False


def test_not_applicable_verdicts_are_excluded_from_the_vote():
    reviews = (
        SpecialistReview(role=SpecialistRole.LEGAL_COUNSEL, verdict=ReviewVerdict.NOT_APPLICABLE, reasoning="no legal dimension"),
        SpecialistReview(role=SpecialistRole.ANALYST, verdict=ReviewVerdict.SUPPORTS, reasoning="pattern consistent"),
    )
    synthesis = synthesize_council_review(reviews)
    assert synthesis.unanimous is True


def test_adversarial_critique_requires_a_real_strongest_criticism():
    import pytest

    with pytest.raises(ValueError):
        adversarial_critique(strongest_criticism="")


def test_relative_risk_without_baseline_is_flagged():
    claim = RelativeRiskClaim(relative_change_pct=200.0, absolute_baseline=None, absolute_change=None)
    assessment = assess_relative_risk_claim(claim)
    assert assessment.flagged is True


def test_relative_risk_with_baseline_is_not_flagged():
    claim = RelativeRiskClaim(relative_change_pct=200.0, absolute_baseline=0.0001, absolute_change=0.0002)
    assessment = assess_relative_risk_claim(claim)
    assert assessment.flagged is False


def test_ten_articles_from_one_source_collapse():
    evidence = tuple(RawEvidence(evidence_id=f"e{i}", state=EvidenceState.OBSERVED, underlying_source_id="wire-story-A") for i in range(10))
    assessment = assess_source_collapse(evidence)
    assert assessment.collapsed is True
    assert assessment.independent_count == 1


def test_ten_genuinely_independent_articles_do_not_collapse():
    evidence = tuple(RawEvidence(evidence_id=f"e{i}", state=EvidenceState.OBSERVED, underlying_source_id=f"source-{i}") for i in range(10))
    assessment = assess_source_collapse(evidence)
    assert assessment.collapsed is False
    assert assessment.independent_count == 10


def test_metric_improving_while_system_quality_worsens_is_conflated():
    assessment = assess_metric_vs_system_quality(metric_name="latency_p50", metric_improved=True, system_quality_evidence="user complaints up 40%", system_quality_worsened=True)
    assert assessment.conflated is True
