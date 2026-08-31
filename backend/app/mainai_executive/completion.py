"""Completion intelligence — CODE WRITTEN != DONE.

Assesses durable evidence states; never treats model claims as completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


COMPLETION_DIMENSIONS = (
    "designed",
    "implemented",
    "tested",
    "security_tested",
    "integrated",
    "runtime_proven",
    "observed",
    "recoverable",
    "documented",
    "production_ready",
)


@dataclass(frozen=True)
class CompletionAssessment:
    feature: str
    dimensions: dict[str, bool]
    false_completion_risk: bool
    summary: str


def assess_completion(
    *,
    feature: str,
    evidence: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Caller supplies evidence flags explicitly — this module never invents VERIFIED."""
    evidence = dict(evidence or {})
    dimensions = {dim: bool(evidence.get(dim, False)) for dim in COMPLETION_DIMENSIONS}
    # CODE WRITTEN != DONE: implemented alone is never production_ready.
    if dimensions["implemented"] and not dimensions["runtime_proven"]:
        dimensions["production_ready"] = False
    if dimensions["implemented"] and not dimensions["tested"]:
        dimensions["production_ready"] = False

    true_count = sum(1 for v in dimensions.values() if v)
    false_risk = dimensions["implemented"] and true_count < 5
    missing = [k for k, v in dimensions.items() if not v]
    summary = (
        f"{feature}: {true_count}/{len(COMPLETION_DIMENSIONS)} dimensions evidenced; "
        f"missing={missing[:5]}"
    )
    return {
        "feature": feature,
        "dimensions": dimensions,
        "false_completion_risk": false_risk,
        "summary": summary,
        "code_written_is_not_done": True,
        "claimed_complete": False,
    }
