"""`app.mainai_coverage.traceability` + `omission_discovery` + `production_claim_check`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

from app.mainai_coverage.omission_discovery import (
    ACTIVATED_NEVER_PRODUCTION_PROVEN,
    BUILT_NEVER_VERIFIED,
    DISCUSSED_NEVER_BUILT,
    VERIFIED_NEVER_INTEGRATED,
    find_omissions,
)
from app.mainai_coverage.production_claim_check import reject_unproven_production_claim
from app.mainai_coverage.traceability import assess_traceability_chain
from app.mainai_coverage.types import CapabilityClaim, CoverageDisposition, MaturityState, TraceabilityStage


def test_full_chain_present_is_complete():
    report = assess_traceability_chain(frozenset(TraceabilityStage))
    assert report.complete is True
    assert report.missing_stages == ()


def test_implemented_but_no_integration_test_finding():
    stages = frozenset({TraceabilityStage.VISION, TraceabilityStage.REQUIREMENT, TraceabilityStage.WHY, TraceabilityStage.ARCHITECTURE, TraceabilityStage.IMPLEMENTATION})
    report = assess_traceability_chain(stages)
    assert report.finding == "Implemented but no integration test."
    assert TraceabilityStage.TEST in report.missing_stages


def test_reviewed_but_not_integrated_finding():
    stages = frozenset(
        {TraceabilityStage.VISION, TraceabilityStage.REQUIREMENT, TraceabilityStage.WHY, TraceabilityStage.ARCHITECTURE,
         TraceabilityStage.IMPLEMENTATION, TraceabilityStage.TEST, TraceabilityStage.REVIEW}
    )
    report = assess_traceability_chain(stages)
    assert report.finding == "Independently reviewed but not integrated."


def test_orphaned_test_with_no_current_implementation_reports_the_real_earlier_gap():
    """A stale/orphaned test recorded without the earlier IMPLEMENTATION stage must not let the
    chain skip ahead -- the real gap (missing implementation) is still reported."""

    stages = frozenset({TraceabilityStage.VISION, TraceabilityStage.REQUIREMENT, TraceabilityStage.TEST})
    report = assess_traceability_chain(stages)
    assert TraceabilityStage.ARCHITECTURE in report.missing_stages
    assert report.furthest_stage == TraceabilityStage.REQUIREMENT


def test_discussed_never_built_omission_is_found():
    claims = (CapabilityClaim(claim_id="c1", description="automatic conversation history omission scanner", mention_count=4),)
    findings = find_omissions(claims=claims, canonical_vision_texts=frozenset({"vision compiler node graph"}))
    assert len(findings) == 1
    assert findings[0].kind == DISCUSSED_NEVER_BUILT
    assert findings[0].recommend_denominator_expansion is True


def test_old_rejected_requirement_is_not_automatically_resurrected():
    claims = (CapabilityClaim(claim_id="c1", description="automatic conversation history omission scanner", mention_count=5, disposition=CoverageDisposition.REJECTED),)
    findings = find_omissions(claims=claims, canonical_vision_texts=frozenset())
    assert findings == ()


def test_single_mention_below_threshold_is_not_flagged():
    claims = (CapabilityClaim(claim_id="c1", description="a passing idea mentioned once", mention_count=1),)
    findings = find_omissions(claims=claims, canonical_vision_texts=frozenset())
    assert findings == ()


def test_implemented_but_unreviewed_capability_is_detected():
    claims = (CapabilityClaim(claim_id="c2", description="already in canonical vision", maturity=MaturityState.IMPLEMENTED),)
    findings = find_omissions(claims=claims, canonical_vision_texts=frozenset({"already in canonical vision"}))
    assert any(f.kind == BUILT_NEVER_VERIFIED for f in findings)


def test_reviewed_but_unintegrated_capability_is_detected():
    claims = (CapabilityClaim(claim_id="c4", description="already in canonical vision", maturity=MaturityState.INDEPENDENTLY_REVIEWED),)
    findings = find_omissions(claims=claims, canonical_vision_texts=frozenset({"already in canonical vision"}))
    assert any(f.kind == VERIFIED_NEVER_INTEGRATED for f in findings)


def test_activated_but_not_production_proven_is_detected():
    claims = (CapabilityClaim(claim_id="c3", description="already in canonical vision", maturity=MaturityState.ACTIVATED),)
    findings = find_omissions(claims=claims, canonical_vision_texts=frozenset({"already in canonical vision"}))
    assert any(f.kind == ACTIVATED_NEVER_PRODUCTION_PROVEN for f in findings)


def test_production_claim_without_runtime_evidence_is_rejected():
    assert reject_unproven_production_claim(maturity=MaturityState.PRODUCTION_PROVEN, has_runtime_evidence=False) is True
    assert reject_unproven_production_claim(maturity=MaturityState.PRODUCTION_PROVEN, has_runtime_evidence=True) is False
    assert reject_unproven_production_claim(maturity=MaturityState.ACTIVATED, has_runtime_evidence=False) is False
