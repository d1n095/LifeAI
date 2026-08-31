"""Executive priority scoring — NOW/NEAR/MID/LONG with hysteresis.

FUTURE PLAN != AUTHORITY. Founder override always wins.
Prevents everything becoming NOW and constant reprioritization thrash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HORIZON_RANK = {"NOW": 4, "NEAR": 3, "MID": 2, "LONG": 1, "LATER": 2, "OPTIONAL": 0, "BLOCKED": -1}
# WorkCandidate.priority vocabulary includes both classic and horizon forms.


@dataclass(frozen=True)
class PriorityFactors:
    urgency: float = 0.5  # 0..1
    importance: float = 0.5
    dependency_blocking: float = 0.0
    risk: float = 0.3
    founder_value: float = 0.5
    cost: float = 0.3  # higher = more expensive → slightly lower priority
    reversibility: float = 0.5  # higher = safer to do now
    confidence: float = 0.5
    unfinished_work: float = 0.0
    known_bug: float = 0.0
    security_severity: float = 0.0
    future_leverage: float = 0.3
    founder_override: str | None = None  # if set, forces that priority


def score_priority(factors: PriorityFactors) -> tuple[str, float, dict[str, Any]]:
    """Return (horizon_priority, raw_score, explanation). Never invents authority."""
    if factors.founder_override:
        ov = factors.founder_override.upper()
        if ov in HORIZON_RANK:
            return ov, 1.0, {"founder_override": True, "forced": ov}

    raw = (
        0.22 * factors.urgency
        + 0.18 * factors.importance
        + 0.14 * factors.dependency_blocking
        + 0.12 * factors.security_severity
        + 0.10 * factors.founder_value
        + 0.08 * factors.known_bug
        + 0.06 * factors.unfinished_work
        + 0.05 * factors.future_leverage
        + 0.04 * factors.reversibility
        + 0.03 * factors.confidence
        - 0.08 * factors.cost
        - 0.05 * max(0.0, factors.risk - 0.5)
    )
    # Soft caps — confidence cannot alone push to NOW.
    if factors.confidence < 0.4 and raw > 0.65:
        raw = 0.65
    # Viral/new input (high urgency alone) cannot dominate without importance/value.
    if factors.urgency > 0.8 and factors.importance < 0.3 and factors.founder_value < 0.3:
        raw = min(raw, 0.55)

    if raw >= 0.78 or factors.security_severity >= 0.85:
        horizon = "NOW"
    elif raw >= 0.58:
        horizon = "NEAR"
    elif raw >= 0.35:
        horizon = "MID"
    else:
        horizon = "LONG"

    return horizon, raw, {
        "raw_score": round(raw, 4),
        "horizon": horizon,
        "founder_override": False,
        "low_confidence_capped": factors.confidence < 0.4,
        "viral_input_damped": factors.urgency > 0.8 and factors.importance < 0.3,
        "authorized": False,
    }


def apply_hysteresis(
    *,
    previous: str | None,
    proposed: str,
    raw_score: float,
    margin: float = 0.08,
) -> str:
    """Resist thrashing between adjacent horizons unless score clearly crosses."""
    if previous is None or previous == proposed:
        return proposed
    prev_r = HORIZON_RANK.get(previous, 0)
    prop_r = HORIZON_RANK.get(proposed, 0)
    if abs(prev_r - prop_r) == 1:
        # Adjacent — require stronger signal to move.
        # Approximate: keep previous unless raw is firmly in new band.
        bands = {"NOW": 0.78, "NEAR": 0.58, "MID": 0.35, "LONG": 0.0}
        threshold = bands.get(proposed, 0.5)
        if prop_r > prev_r and raw_score < threshold + margin:
            return previous
        if prop_r < prev_r and raw_score > threshold - margin:
            return previous
    return proposed
