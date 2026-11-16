"""MainAI Research -- `app.mainai_research.epistemic_caution` -- proves LOW EXPERIENCE / LOW
EVIDENCE raises the confidence bar, and that genuinely overwhelming independent evidence is
never turned into fake uncertainty.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

from app.mainai_research.epistemic_caution import CautionAction, assess_epistemic_caution


def test_sparse_evidence_and_thin_history_raises_the_bar_and_delays_conclusion():
    assessment = assess_epistemic_caution(independent_source_count=1, history_depth=1)
    assert CautionAction.DELAY_STRONG_CONCLUSION in assessment.required_actions
    assert CautionAction.WIDEN_SEARCH in assessment.required_actions
    assert assessment.raised_confidence_bar > 0.5


def test_97_percent_independent_high_quality_evidence_justifies_a_strong_conclusion_despite_thin_history():
    """THE explicit contrast case: even with a young system / thin history, overwhelming
    high-quality independent evidence must NOT be diluted into artificial 50/50 caution."""
    assessment = assess_epistemic_caution(
        independent_source_count=1, history_depth=0, high_quality_independent_evidence_fraction=0.97,
    )
    assert assessment.evidence_is_overwhelming is True
    assert assessment.required_actions == (CautionAction.PROCEED,)
    assert assessment.raised_confidence_bar == 0.5


def test_high_source_dependence_raises_the_bar_even_with_deep_history():
    assessment = assess_epistemic_caution(independent_source_count=5, history_depth=50, source_dependence_fraction=0.9)
    assert assessment.raised_confidence_bar > 0.5


def test_rich_evidence_and_history_proceeds_at_base_bar():
    assessment = assess_epistemic_caution(independent_source_count=5, history_depth=50)
    assert assessment.required_actions == (CautionAction.PROCEED,)
    assert assessment.raised_confidence_bar == 0.5


def test_missing_important_context_alone_triggers_caution():
    assessment = assess_epistemic_caution(independent_source_count=10, history_depth=100, missing_important_context=True)
    assert CautionAction.DELAY_STRONG_CONCLUSION in assessment.required_actions
