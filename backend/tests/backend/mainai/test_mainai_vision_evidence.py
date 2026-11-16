"""MainAI Cognitive Control Plane -- `app.mainai_vision.evidence` -- proves REPEATED SOURCE !=
INDEPENDENT EVIDENCE, AUTHORITY != EVIDENCE (by omission -- no field anywhere in this module
represents "who said it" as evidentiary weight), the deterministic claim-state classification
table, and confound/denominator disclosure reasoning.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

from app.mainai_vision.evidence import (
    EvidenceClaim,
    EvidenceState,
    RawEvidence,
    classify_claim_state,
    count_independent_sources,
    evaluate_confounds,
)


def _evidence(source_id, *, state=EvidenceState.OBSERVED, **kwargs) -> RawEvidence:
    return RawEvidence(evidence_id=f"e-{source_id}-{kwargs.get('notes', '')}", state=state, underlying_source_id=source_id, **kwargs)


# ============================================================================ REPEATED SOURCE != INDEPENDENT EVIDENCE


def test_ten_citations_of_one_source_count_as_one_independent_source():
    evidence = tuple(_evidence("study-A", notes=str(i)) for i in range(10))
    assert count_independent_sources(evidence) == 1


def test_two_genuinely_different_sources_count_as_two():
    evidence = (_evidence("study-A"), _evidence("study-B"))
    assert count_independent_sources(evidence) == 2


def test_repeated_source_never_promotes_past_inferred():
    """POPULAR CONSENSUS != PROOF: 20 citations of the same source stay INFERRED, not DERIVED."""
    claim = EvidenceClaim(claim="x", support=tuple(_evidence("study-A", notes=str(i)) for i in range(20)))
    assert classify_claim_state(claim) == EvidenceState.INFERRED


def test_two_independent_sources_promotes_to_derived():
    claim = EvidenceClaim(claim="x", support=(_evidence("study-A"), _evidence("study-B")))
    assert classify_claim_state(claim) == EvidenceState.DERIVED


# ============================================================================ classification table


def test_no_support_is_speculative():
    assert classify_claim_state(EvidenceClaim(claim="x")) == EvidenceState.SPECULATIVE


def test_verified_support_with_no_contradiction_is_verified():
    claim = EvidenceClaim(claim="x", support=(_evidence("s1", state=EvidenceState.VERIFIED),))
    assert classify_claim_state(claim) == EvidenceState.VERIFIED


def test_contradiction_overrides_even_strong_support():
    claim = EvidenceClaim(
        claim="x",
        support=(_evidence("s1", state=EvidenceState.VERIFIED), _evidence("s2"), _evidence("s3")),
        contradiction=(_evidence("s4"),),
    )
    assert classify_claim_state(claim) == EvidenceState.CONTRADICTED


def test_minority_contradicting_claim_is_not_suppressed():
    """MINORITY CLAIM != SUPPRESSED TRUTH: one real contradiction against many supporting items
    (even independent ones) still flips the state to CONTRADICTED, never averaged away."""
    many_support = tuple(_evidence(f"s{i}") for i in range(20))
    claim = EvidenceClaim(claim="x", support=many_support, contradiction=(_evidence("lone-dissent"),))
    assert classify_claim_state(claim) == EvidenceState.CONTRADICTED


# ============================================================================ confound / denominator disclosure


def test_missing_sample_size_is_undisclosed_not_assumed_clean():
    ev = _evidence("s1", sample_size=None, population="adults", time_window="2024")
    check = evaluate_confounds(ev)
    assert check.denominator_disclosed is False
    assert check.fully_disclosed is False


def test_fully_disclosed_evidence_with_no_named_risks_is_fully_disclosed():
    ev = _evidence("s1", sample_size=500, population="adults", time_window="2024")
    check = evaluate_confounds(ev, absolute_and_relative_both_given=True)
    assert check.fully_disclosed is True
    assert check.open_risks == ()


def test_named_risk_is_surfaced_not_silently_dropped():
    ev = _evidence("s1", sample_size=500, population="adults", time_window="2024")
    check = evaluate_confounds(ev, absolute_and_relative_both_given=True, confounding_risk="unadjusted age")
    assert "unadjusted age" in check.open_risks


def test_unassessed_risk_is_none_never_fabricated_as_absent():
    ev = _evidence("s1", sample_size=500)
    check = evaluate_confounds(ev)
    assert check.confounding_risk is None
    assert check.selection_risk is None
