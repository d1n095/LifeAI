"""Context Atlas — read-only source identity, system map, traceability and recommendations.

This package deliberately adds NO memory store, vector index, queue or agent loop. It is a
thin, bounded analysis layer over artifacts that already exist (Document.checksum,
app/storage/ content-addressed blobs, KnowledgeVersion, SourceRelationship, the checkout's
own import graph) plus repository evidence. See docs/CONTEXT_ATLAS.md.
"""

from app.context_atlas.candidates import (
    CandidateReport,
    SourceCandidate,
    find_candidates,
)
from app.context_atlas.capability_map import Capability, CapabilityMap, scan_capabilities
from app.context_atlas.git_evidence import (
    GitEvidence,
    GitRef,
    PullRequestRef,
    load_pull_requests,
    scan_local_git,
)
from app.context_atlas.identity import (
    IdentityAssessment,
    MediaKind,
    SourceFingerprint,
    Verdict,
    compare_sources,
    fingerprint,
)
from app.context_atlas.recommendation import (
    CAPABILITY_ALIASES,
    Alternative,
    CapabilityFact,
    EngineeringRequest,
    Recommendation,
    RecommendationOutcome,
    match_capabilities,
    recommend,
)
from app.context_atlas.reconciliation import (
    ContextObservation,
    Decision,
    Outcome,
    reconcile,
)
from app.context_atlas.relationships import (
    CONCEPT_MAPPINGS,
    EvidenceKind,
    RelationshipProposal,
    propose_relationship,
)
from app.context_atlas.system_map import (
    ChangeImpact,
    Endpoint,
    Maturity,
    ModuleNode,
    SystemMap,
    scan_system_map,
)
from app.context_atlas.traceability import (
    STAGE_ORDER,
    ContextClass,
    Disagreement,
    EvidenceRef,
    LinkStatus,
    RequirementTrace,
    SourceStatement,
    Stage,
    TraceLink,
    build_trace,
    reconcile_disagreement,
)

__all__ = [
    "CAPABILITY_ALIASES",
    "CONCEPT_MAPPINGS",
    "STAGE_ORDER",
    "Alternative",
    "CandidateReport",
    "Capability",
    "CapabilityFact",
    "CapabilityMap",
    "ChangeImpact",
    "ContextClass",
    "ContextObservation",
    "Decision",
    "Disagreement",
    "Endpoint",
    "EngineeringRequest",
    "EvidenceKind",
    "EvidenceRef",
    "GitEvidence",
    "GitRef",
    "IdentityAssessment",
    "LinkStatus",
    "Maturity",
    "MediaKind",
    "ModuleNode",
    "Outcome",
    "PullRequestRef",
    "Recommendation",
    "RecommendationOutcome",
    "RelationshipProposal",
    "RequirementTrace",
    "SourceCandidate",
    "SourceFingerprint",
    "SourceStatement",
    "Stage",
    "SystemMap",
    "TraceLink",
    "Verdict",
    "build_trace",
    "compare_sources",
    "find_candidates",
    "fingerprint",
    "load_pull_requests",
    "match_capabilities",
    "propose_relationship",
    "recommend",
    "reconcile",
    "reconcile_disagreement",
    "scan_capabilities",
    "scan_local_git",
    "scan_system_map",
]
