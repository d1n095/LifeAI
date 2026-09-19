"""Words vs Actions -- longitudinal statement/action comparison. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

LINGUISTIC PATTERN != PROOF OF INTENT: a repeated mismatch is a signal for deeper investigation,
never automatic guilt -- structurally, this module's own output has no "intent"/"guilt" field
at all, only `mismatch_count`/`pattern_detected`/`recommend_deeper_investigation`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StatementActionPair:
    statement: str
    statement_at: datetime
    action: str
    action_at: datetime
    matches: bool
    notes: str | None = None


@dataclass(frozen=True)
class WordsActionsAssessment:
    actor_id: str
    pairs: tuple[StatementActionPair, ...]
    mismatch_count: int
    mismatch_fraction: float
    pattern_detected: bool
    recommend_deeper_investigation: bool
    reason: str


# Below this many observed pairs, a "pattern" cannot yet be distinguished from noise -- same
# small-sample discipline as `resource_intelligence.efficiency_profile.MIN_SAMPLE_SIZE_FOR_
# ESTABLISHED` (documented, hand-picked, not fit to data that does not exist yet).
MIN_PAIRS_FOR_PATTERN = 3
PATTERN_MISMATCH_FRACTION = 0.5


def assess_words_vs_actions(*, actor_id: str, pairs: tuple[StatementActionPair, ...]) -> WordsActionsAssessment:
    """Pure. A repeated mismatch (>= `MIN_PAIRS_FOR_PATTERN` observed pairs AND mismatch
    fraction >= `PATTERN_MISMATCH_FRACTION`) recommends deeper investigation -- it is never
    itself a conclusion about intent."""

    mismatches = sum(1 for p in pairs if not p.matches)
    fraction = (mismatches / len(pairs)) if pairs else 0.0
    pattern = len(pairs) >= MIN_PAIRS_FOR_PATTERN and fraction >= PATTERN_MISMATCH_FRACTION

    if not pairs:
        reason = "no statement/action pairs observed yet -- no pattern to assess"
    elif len(pairs) < MIN_PAIRS_FOR_PATTERN:
        reason = f"only {len(pairs)} pair(s) observed (< {MIN_PAIRS_FOR_PATTERN}) -- too few to distinguish a pattern from noise"
    elif pattern:
        reason = (
            f"{mismatches}/{len(pairs)} ({fraction:.0%}) statement/action pairs mismatch -- "
            "LINGUISTIC PATTERN != PROOF OF INTENT: this recommends deeper investigation, it is not a conclusion"
        )
    else:
        reason = f"{mismatches}/{len(pairs)} ({fraction:.0%}) mismatch -- below the pattern threshold"

    return WordsActionsAssessment(
        actor_id=actor_id, pairs=pairs, mismatch_count=mismatches, mismatch_fraction=fraction,
        pattern_detected=pattern, recommend_deeper_investigation=pattern, reason=reason,
    )
