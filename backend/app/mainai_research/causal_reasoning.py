"""Causal / Logical Reasoning -- for a recurring relationship X/Y -> D, explicitly test every
named alternative before accepting a causal reading. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

RELATIONSHIP != CONTROL. BENEFIT != PROOF OF INTENT. CONTACT != CONSPIRACY. CORRELATION !=
CAUSATION -- structurally: this module has no function that outputs "causation" from
correlation alone; every causal-leaning result requires the caller to have explicitly checked
(and recorded the outcome of) each `CausalTest`."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_research.types import CausalTest


@dataclass(frozen=True)
class CausalTestOutcome:
    test: CausalTest
    ruled_out: bool  # True if this alternative explanation was checked and ruled out
    notes: str | None = None


@dataclass(frozen=True)
class CausalAssessment:
    relationship_description: str
    outcomes: tuple[CausalTestOutcome, ...]
    all_alternatives_ruled_out: bool
    unruled_out: tuple[CausalTest, ...]
    verdict: str  # "causal_reading_supported" | "correlation_only" | "insufficient_testing"


REQUIRED_TESTS: tuple[CausalTest, ...] = (
    CausalTest.COMMON_CAUSE, CausalTest.SELECTION_EFFECT, CausalTest.REVERSE_CAUSATION,
    CausalTest.COINCIDENCE, CausalTest.HIDDEN_VARIABLE, CausalTest.TEMPORAL_ORDER,
)


def assess_causal_claim(*, relationship_description: str, outcomes: tuple[CausalTestOutcome, ...]) -> CausalAssessment:
    """Pure. A causal reading is supported ONLY when every one of `REQUIRED_TESTS` was actually
    checked (present in `outcomes`) and ruled out -- an untested alternative is treated as
    UNRULED OUT, never assumed absent (MISSING != ZERO's sibling here)."""

    checked = {o.test: o for o in outcomes}
    missing = tuple(t for t in REQUIRED_TESTS if t not in checked)
    not_ruled_out = tuple(t for t in REQUIRED_TESTS if t in checked and not checked[t].ruled_out)
    unruled_out = missing + not_ruled_out

    if not outcomes:
        verdict = "insufficient_testing"
    elif unruled_out:
        verdict = "correlation_only"
    else:
        verdict = "causal_reading_supported"

    return CausalAssessment(
        relationship_description=relationship_description, outcomes=outcomes,
        all_alternatives_ruled_out=not unruled_out, unruled_out=unruled_out, verdict=verdict,
    )
