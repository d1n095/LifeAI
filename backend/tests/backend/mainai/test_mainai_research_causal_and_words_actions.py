"""MainAI Research -- `app.mainai_research.causal_reasoning` + `words_vs_actions` -- proves
CORRELATION != CAUSATION structurally (a causal reading requires every alternative explanation
to be checked and ruled out) and LINGUISTIC PATTERN != PROOF OF INTENT.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture these
modules implement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.mainai_research.causal_reasoning import REQUIRED_TESTS, CausalTestOutcome, assess_causal_claim
from app.mainai_research.types import CausalTest
from app.mainai_research.words_vs_actions import StatementActionPair, assess_words_vs_actions


def test_bare_correlation_with_no_tests_is_insufficient_testing():
    assessment = assess_causal_claim(relationship_description="X correlates with D", outcomes=())
    assert assessment.verdict == "insufficient_testing"
    assert assessment.all_alternatives_ruled_out is False


def test_partial_testing_is_correlation_only_not_causal():
    outcomes = (CausalTestOutcome(test=CausalTest.COMMON_CAUSE, ruled_out=True),)
    assessment = assess_causal_claim(relationship_description="X correlates with D", outcomes=outcomes)
    assert assessment.verdict == "correlation_only"
    assert CausalTest.REVERSE_CAUSATION in assessment.unruled_out


def test_all_required_tests_ruled_out_supports_a_causal_reading():
    outcomes = tuple(CausalTestOutcome(test=t, ruled_out=True) for t in REQUIRED_TESTS)
    assessment = assess_causal_claim(relationship_description="X causes D", outcomes=outcomes)
    assert assessment.verdict == "causal_reading_supported"
    assert assessment.all_alternatives_ruled_out is True


def test_one_unruled_out_test_blocks_causal_verdict_even_if_others_pass():
    outcomes = tuple(
        CausalTestOutcome(test=t, ruled_out=(t != CausalTest.REVERSE_CAUSATION)) for t in REQUIRED_TESTS
    )
    assessment = assess_causal_claim(relationship_description="X causes D", outcomes=outcomes)
    assert assessment.verdict == "correlation_only"
    assert assessment.unruled_out == (CausalTest.REVERSE_CAUSATION,)


def _pair(matches: bool, i: int) -> StatementActionPair:
    now = datetime.now(timezone.utc)
    return StatementActionPair(statement=f"s{i}", statement_at=now - timedelta(days=i), action=f"a{i}", action_at=now, matches=matches)


def test_too_few_pairs_never_claims_a_pattern():
    assessment = assess_words_vs_actions(actor_id="actor-1", pairs=(_pair(False, 1), _pair(False, 2)))
    assert assessment.pattern_detected is False


def test_repeated_mismatch_recommends_deeper_investigation_not_a_conclusion():
    pairs = tuple(_pair(False, i) for i in range(4))
    assessment = assess_words_vs_actions(actor_id="actor-1", pairs=pairs)
    assert assessment.pattern_detected is True
    assert assessment.recommend_deeper_investigation is True
    assert not hasattr(assessment, "intent")
    assert not hasattr(assessment, "guilt")


def test_consistent_match_never_recommends_investigation():
    pairs = tuple(_pair(True, i) for i in range(5))
    assessment = assess_words_vs_actions(actor_id="actor-1", pairs=pairs)
    assert assessment.pattern_detected is False
