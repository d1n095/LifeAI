"""MainAI Coverage & Omission Intelligence. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md for the architecture
decision."""

from __future__ import annotations

from app.mainai_coverage.dynamic_denominator import DenominatorExpansionResult, stage_omission_for_vision_expansion
from app.mainai_coverage.omission_discovery import (
    ACTIVATED_NEVER_PRODUCTION_PROVEN,
    BUILT_NEVER_VERIFIED,
    DISCUSSED_NEVER_BUILT,
    VERIFIED_NEVER_INTEGRATED,
    OmissionFinding,
    find_omissions,
)
from app.mainai_coverage.production_claim_check import reject_unproven_production_claim
from app.mainai_coverage.traceability import TraceabilityGapReport, assess_traceability_chain
from app.mainai_coverage.types import (
    MATURITY_INDEX,
    MATURITY_ORDER,
    CapabilityClaim,
    CoverageDisposition,
    CoverageError,
    MaturityState,
    TraceabilityStage,
    maturity_at_least,
)

__all__ = [
    "ACTIVATED_NEVER_PRODUCTION_PROVEN",
    "BUILT_NEVER_VERIFIED",
    "CapabilityClaim",
    "CoverageDisposition",
    "CoverageError",
    "DISCUSSED_NEVER_BUILT",
    "DenominatorExpansionResult",
    "MATURITY_INDEX",
    "MATURITY_ORDER",
    "MaturityState",
    "OmissionFinding",
    "TraceabilityGapReport",
    "TraceabilityStage",
    "VERIFIED_NEVER_INTEGRATED",
    "assess_traceability_chain",
    "find_omissions",
    "maturity_at_least",
    "reject_unproven_production_claim",
    "stage_omission_for_vision_expansion",
]
