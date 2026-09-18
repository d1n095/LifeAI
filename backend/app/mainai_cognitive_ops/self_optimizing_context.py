"""Self-Optimizing Context. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

LESS CONTEXT != BETTER. MORE CONTEXT != BETTER. FASTER != BETTER IF QUALITY FALLS. CHEAPER !=
BETTER IF REWORK RISES. SELF-OPTIMIZATION MUST BE MEASURED AGAINST VERIFIED OUTCOMES.

Pure: compares two caller-supplied `ContextStrategyMetrics` snapshots (baseline vs candidate
packaging strategy) and decides whether the candidate should be adopted."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextStrategyMetrics:
    tokens_used: int
    retrieval_latency_ms: float
    reread_count: int
    missed_dependencies: int
    retries: int
    rework_count: int
    examiner_failures: int
    wall_clock_seconds: float
    cost_usd: float
    founder_corrections: int
    verified_outcomes: int


@dataclass(frozen=True)
class StrategyEvaluation:
    accepted: bool
    reason: str


def evaluate_strategy_change(baseline: ContextStrategyMetrics, candidate: ContextStrategyMetrics) -> StrategyEvaluation:
    """Quality signals (examiner_failures, founder_corrections, missed_dependencies,
    verified_outcomes) gate acceptance FIRST -- any quality regression rejects the candidate
    regardless of how much cheaper/faster/leaner it is. Only once quality holds or improves do
    efficiency gains (tokens, latency, reread, rework, retries, cost, wall-clock) get counted."""

    quality_regressed = (
        candidate.examiner_failures > baseline.examiner_failures
        or candidate.founder_corrections > baseline.founder_corrections
        or candidate.missed_dependencies > baseline.missed_dependencies
        or candidate.verified_outcomes < baseline.verified_outcomes
    )
    if quality_regressed:
        return StrategyEvaluation(accepted=False, reason="quality regressed (examiner failures, founder corrections, missed dependencies, or verified outcomes worsened) -- rejected regardless of any efficiency gain")

    efficiency_improved = (
        candidate.reread_count < baseline.reread_count
        or candidate.rework_count < baseline.rework_count
        or candidate.retries < baseline.retries
    )
    if efficiency_improved:
        return StrategyEvaluation(accepted=True, reason="quality held or improved, and reread/rework/retries decreased")

    if candidate.tokens_used < baseline.tokens_used or candidate.cost_usd < baseline.cost_usd or candidate.wall_clock_seconds < baseline.wall_clock_seconds:
        return StrategyEvaluation(accepted=True, reason="quality held or improved, and at least one of tokens/cost/wall-clock decreased with no rework/retry increase")

    return StrategyEvaluation(accepted=False, reason="no measurable efficiency gain over baseline with matching quality -- not adopted for its own sake")
