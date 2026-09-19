"""Early-Stage Epistemic Caution -- MainAI is still young and must avoid premature conclusions.
See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture
decision.

Pure: no `db`, no I/O. LOW EXPERIENCE / LOW EVIDENCE -> HIGHER THRESHOLD FOR STRONG CLAIMS --
but this module never fabricates doubt against evidence that is genuinely overwhelming (see
`test_97_percent_independent_evidence_still_justifies_a_strong_conclusion` for the contrast
case)."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class CautionAction(str, enum.Enum):
    WIDEN_SEARCH = "widen_search"
    DELAY_STRONG_CONCLUSION = "delay_strong_conclusion"
    SEEK_MORE_INDEPENDENT_SOURCES = "seek_more_independent_sources"
    SEEK_PRIMARY_SOURCES = "seek_primary_sources"
    MAP_ACTORS = "map_actors"
    MAP_MONEY = "map_money"
    MAP_RELATIONSHIPS = "map_relationships"
    BUILD_TIMELINE = "build_timeline"
    COMPARE_WORDS_VS_ACTIONS = "compare_words_vs_actions"
    SEARCH_COUNTEREVIDENCE = "search_counterevidence"
    CHECK_UNKNOWNS = "check_unknowns"
    PROCEED = "proceed"  # sparse-evidence conditions do not apply -- normal confidence bar holds


# Documented, hand-picked thresholds -- same convention as every other threshold table in this
# program (`judgment.py`'s CONFIDENCE_BAR, `decision.py`'s CONTEXT_UTILIZATION_*_PCT). No
# historical data exists yet to fit these against.
SPARSE_INDEPENDENT_SOURCE_COUNT = 2  # fewer than this independent sources is "sparse"
SPARSE_HISTORY_DEPTH = 3  # fewer than this many prior, comparable investigations is "sparse"
HIGH_SOURCE_DEPENDENCE_FRACTION = 0.7  # this fraction of citations sharing one underlying source
OVERWHELMING_INDEPENDENT_FRACTION = 0.95  # high-quality, independent evidence fraction this strong justifies a strong conclusion despite thin history


@dataclass(frozen=True)
class CautionAssessment:
    required_actions: tuple[CautionAction, ...]
    raised_confidence_bar: float  # the effective CONFIDENCE_BAR this investigation should use (0..1)
    reason: str
    evidence_is_overwhelming: bool = False


def assess_epistemic_caution(
    *,
    independent_source_count: int,
    history_depth: int,
    source_dependence_fraction: float | None = None,
    missing_important_context: bool = False,
    base_confidence_bar: float = 0.5,
    high_quality_independent_evidence_fraction: float | None = None,
) -> CautionAssessment:
    """Pure decision table -- the FIRST matching rule wins, same convention as
    `judgment.decide_judgment()`/`resource_intelligence.decision.propose_resource_action()`.

    1. `high_quality_independent_evidence_fraction >= OVERWHELMING_INDEPENDENT_FRACTION` ->
       PROCEED at the BASE bar, regardless of how sparse this system's own history is. DO NOT
       CREATE FAKE UNCERTAINTY WHEN EVIDENCE IS GENUINELY OVERWHELMING.
    2. Otherwise, if independent sources are sparse, history is thin, source dependence is high,
       or important context is missing -> raise the confidence bar and require the caller to
       widen its search before treating any conclusion as strong.
    3. Otherwise -> PROCEED at the base bar.
    """

    if high_quality_independent_evidence_fraction is not None and high_quality_independent_evidence_fraction >= OVERWHELMING_INDEPENDENT_FRACTION:
        return CautionAssessment(
            required_actions=(CautionAction.PROCEED,),
            raised_confidence_bar=base_confidence_bar,
            reason=(
                f"high_quality_independent_evidence_fraction={high_quality_independent_evidence_fraction:.2f} "
                f">= {OVERWHELMING_INDEPENDENT_FRACTION} -- evidence is genuinely overwhelming; "
                "DO NOT create fake uncertainty by artificially raising the bar"
            ),
            evidence_is_overwhelming=True,
        )

    sparse_sources = independent_source_count < SPARSE_INDEPENDENT_SOURCE_COUNT
    sparse_history = history_depth < SPARSE_HISTORY_DEPTH
    high_dependence = source_dependence_fraction is not None and source_dependence_fraction >= HIGH_SOURCE_DEPENDENCE_FRACTION

    if sparse_sources or sparse_history or high_dependence or missing_important_context:
        actions = [CautionAction.WIDEN_SEARCH, CautionAction.DELAY_STRONG_CONCLUSION]
        if sparse_sources:
            actions.append(CautionAction.SEEK_MORE_INDEPENDENT_SOURCES)
            actions.append(CautionAction.SEEK_PRIMARY_SOURCES)
        actions.extend([
            CautionAction.MAP_ACTORS, CautionAction.MAP_MONEY, CautionAction.MAP_RELATIONSHIPS,
            CautionAction.BUILD_TIMELINE, CautionAction.COMPARE_WORDS_VS_ACTIONS,
            CautionAction.SEARCH_COUNTEREVIDENCE, CautionAction.CHECK_UNKNOWNS,
        ])
        raised_bar = min(0.9, base_confidence_bar + 0.2)
        reasons = []
        if sparse_sources:
            reasons.append(f"independent_source_count={independent_source_count} < {SPARSE_INDEPENDENT_SOURCE_COUNT}")
        if sparse_history:
            reasons.append(f"history_depth={history_depth} < {SPARSE_HISTORY_DEPTH}")
        if high_dependence:
            reasons.append(f"source_dependence_fraction={source_dependence_fraction:.2f} >= {HIGH_SOURCE_DEPENDENCE_FRACTION}")
        if missing_important_context:
            reasons.append("missing_important_context=True")
        return CautionAssessment(
            required_actions=tuple(actions),
            raised_confidence_bar=raised_bar,
            reason="LOW EXPERIENCE / LOW EVIDENCE -> HIGHER THRESHOLD: " + "; ".join(reasons),
        )

    return CautionAssessment(
        required_actions=(CautionAction.PROCEED,),
        raised_confidence_bar=base_confidence_bar,
        reason="evidence base is not sparse by any tracked signal -- normal confidence bar holds",
    )
