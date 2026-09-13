"""Production-claim guard. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

ACTIVATED != PRODUCTION_PROVEN: a claim of PRODUCTION_PROVEN maturity with no real runtime
evidence attached is rejected outright, never accepted on the strength of the maturity label
alone."""

from __future__ import annotations

from app.mainai_coverage.types import MATURITY_INDEX, MaturityState


def reject_unproven_production_claim(*, maturity: MaturityState, has_runtime_evidence: bool) -> bool:
    """True means REJECT this claim of PRODUCTION_PROVEN (or higher) status. A claim below
    PRODUCTION_PROVEN is never rejected by this function regardless of runtime evidence -- it
    only guards the one rung where the label itself asserts real-world proof."""

    if MATURITY_INDEX[maturity] < MATURITY_INDEX[MaturityState.PRODUCTION_PROVEN]:
        return False
    return not has_runtime_evidence
