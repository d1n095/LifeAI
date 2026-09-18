"""MainAI Coverage & Omission Intelligence. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md for the architecture
decision."""

from __future__ import annotations

from app.mainai_coverage.discovery_pipeline import DiscoveryRunReport, run_discovery
from app.mainai_coverage.dynamic_denominator import DenominatorExpansionResult, stage_omission_for_vision_expansion
from app.mainai_coverage.matching import MatchResult, layered_match, semantic_similarity_available
from app.mainai_coverage.omission_discovery import (
    ACTIVATED_NEVER_PRODUCTION_PROVEN,
    BUILT_NEVER_VERIFIED,
    DISCUSSED_NEVER_BUILT,
    VERIFIED_NEVER_INTEGRATED,
    OmissionFinding,
    find_omissions,
)
from app.mainai_coverage.production_claim_check import reject_unproven_production_claim
from app.mainai_coverage.requirement_extraction import build_claims_from_observations, deduplicate_claims, extract_capability_claims
from app.mainai_coverage.source_adapters import (
    SourceAdapterError,
    conversation_history_availability,
    discover_docs,
    ingest_branch_registry,
    ingest_git_commit_log,
    ingest_markdown_doc,
    ingest_test_evidence,
)
from app.mainai_coverage.traceability import TraceabilityGapReport, assess_traceability_chain
from app.mainai_coverage.types import (
    MATURITY_INDEX,
    MATURITY_ORDER,
    CapabilityClaim,
    CoverageDisposition,
    CoverageError,
    ExtractionMethod,
    MatchLayer,
    MaturityState,
    NormalizedObservation,
    SourceAvailability,
    SourceKind,
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
    "DiscoveryRunReport",
    "ExtractionMethod",
    "MATURITY_INDEX",
    "MATURITY_ORDER",
    "MatchLayer",
    "MatchResult",
    "MaturityState",
    "NormalizedObservation",
    "OmissionFinding",
    "SourceAdapterError",
    "SourceAvailability",
    "SourceKind",
    "TraceabilityGapReport",
    "TraceabilityStage",
    "VERIFIED_NEVER_INTEGRATED",
    "assess_traceability_chain",
    "build_claims_from_observations",
    "conversation_history_availability",
    "deduplicate_claims",
    "discover_docs",
    "extract_capability_claims",
    "find_omissions",
    "ingest_branch_registry",
    "ingest_git_commit_log",
    "ingest_markdown_doc",
    "ingest_test_evidence",
    "layered_match",
    "maturity_at_least",
    "reject_unproven_production_claim",
    "run_discovery",
    "semantic_similarity_available",
    "stage_omission_for_vision_expansion",
]
