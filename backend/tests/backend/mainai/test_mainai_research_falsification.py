"""MainAI Research -- `app.mainai_research.falsification` -- proves the recursive falsification
engine: surviving raises confidence (bounded), a real contradiction caps it low regardless of
prior support, failure to find counterevidence is never treated as proof, and confidence never
exceeds the survival-alone ceiling.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

from app.mainai_research.falsification import (
    CONTRADICTION_CONFIDENCE_FLOOR,
    MAX_CONFIDENCE_FROM_SURVIVAL_ALONE,
    failure_to_find_counterevidence_is_not_proof,
    run_falsification_round,
)


def test_surviving_a_round_raises_confidence():
    result = run_falsification_round(current_confidence=0.5, round_number=1, survived=True, counterevidence_found=False)
    assert result.new_confidence > 0.5
    assert result.new_hypothesis_status == "survived_falsification"


def test_surviving_repeatedly_never_exceeds_the_survival_ceiling():
    confidence = 0.5
    for round_number in range(1, 20):
        result = run_falsification_round(current_confidence=confidence, round_number=round_number, survived=True, counterevidence_found=False)
        confidence = result.new_confidence
    assert confidence <= MAX_CONFIDENCE_FROM_SURVIVAL_ALONE


def test_real_contradiction_caps_confidence_low_regardless_of_high_prior_support():
    result = run_falsification_round(current_confidence=0.95, round_number=5, survived=True, counterevidence_found=True, real_contradiction_present=True)
    assert result.new_confidence <= CONTRADICTION_CONFIDENCE_FLOOR
    assert result.new_hypothesis_status == "contradicted"


def test_failing_to_survive_lowers_confidence_but_does_not_contradict():
    result = run_falsification_round(current_confidence=0.5, round_number=2, survived=False, counterevidence_found=True)
    assert result.new_confidence < 0.5
    assert result.new_hypothesis_status == "active"


def test_failure_to_find_counterevidence_is_never_proof():
    assert failure_to_find_counterevidence_is_not_proof(counterevidence_found=False) is True
    assert failure_to_find_counterevidence_is_not_proof(counterevidence_found=True) is False


def test_diminishing_returns_flagged_when_marginal_gain_is_small():
    result = run_falsification_round(current_confidence=0.89, round_number=10, survived=True, counterevidence_found=False)
    assert result.diminishing_returns is True
