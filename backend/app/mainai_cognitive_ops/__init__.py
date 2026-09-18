"""MainAI Cognitive Efficiency + Systemic Debugging + Situational Awareness + Information
Lifecycle + Repo/Backup Intelligence. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md for the architecture decision."""

from __future__ import annotations

from app.mainai_cognitive_ops.change_impact import ImpactEstimate, ImpactVerification, estimate_impact, select_regression_scope, verify_impact
from app.mainai_cognitive_ops.compatibility_graph import CompatibilityEdge, CompatibilityGraph, blast_radius, build_compatibility_graph
from app.mainai_cognitive_ops.compression import RecoveryCheckpoint, parse_handoff_markdown, validate_checkpoint_reconstructability
from app.mainai_cognitive_ops.context_packaging import (
    CodeDebugPackage,
    FounderDecisionPackage,
    LegalPackage,
    ResearchPackage,
    build_code_debug_package,
    build_founder_decision_package,
    build_legal_package,
    build_research_package,
)
from app.mainai_cognitive_ops.duplication_control import DuplicationAssessment, assess_duplication
from app.mainai_cognitive_ops.founder_anti_repetition import CommunicationNecessity, assess_communication_necessity
from app.mainai_cognitive_ops.founder_communication_ledger import latest_communication_for_topic, list_communications_for_topic, record_communication
from app.mainai_cognitive_ops.hot_warm_cold import TierTransition, classify_temperature, transition_tier
from app.mainai_cognitive_ops.indexing import IndexDefinition, RetrievalEffectiveness, RetrievalEvent, assess_retrieval_effectiveness, detect_stale_index
from app.mainai_cognitive_ops.information_lifecycle import DeduplicationResult, DefragmentedBundle, InformationItem, deduplicate_exact, defragment, fragment
from app.mainai_cognitive_ops.provider_economics_bridge import derive_provider_economics_signal
from app.mainai_cognitive_ops.repo_backup_intelligence import (
    BackupRiskAssessment,
    GitIntrospectionError,
    assess_backup_risk,
    get_ahead_behind,
    get_current_branch,
    get_dirty_files,
    get_local_head,
    get_remote_tracking_sha,
    snapshot_remote_sync_state,
)
from app.mainai_cognitive_ops.research_graph_provenance import build_containment_query_fragment, validate_graph_provenance_payload
from app.mainai_cognitive_ops.research_reopen_trigger import (
    ReopenCandidate,
    assess_reopen_candidate,
    find_cross_investigation_reopen_candidates,
    score_relevance,
    trigger_cross_investigation_reopen,
)
from app.mainai_cognitive_ops.self_optimizing_context import ContextStrategyMetrics, StrategyEvaluation, evaluate_strategy_change
from app.mainai_cognitive_ops.situational_awareness import (
    ProgramCompletionClaim,
    assess_program_completion_claim,
    filter_known_active_work,
    is_assignable,
    select_assignable_agents,
)
from app.mainai_cognitive_ops.systemic_debugging import DebugTrace, FixReadiness, assess_fix_readiness, claims_local_correctness_only, record_stage
from app.mainai_cognitive_ops.types import (
    AgentState,
    CognitiveOpsError,
    CommunicationDeltaVerdict,
    DebugStage,
    DuplicationVerdict,
    InformationTemperature,
    InformationTier,
    ProgramStatus,
    RemoteSyncState,
    WorkItem,
)

__all__ = [
    "AgentState",
    "BackupRiskAssessment",
    "CodeDebugPackage",
    "CognitiveOpsError",
    "CommunicationDeltaVerdict",
    "CommunicationNecessity",
    "CompatibilityEdge",
    "CompatibilityGraph",
    "ContextStrategyMetrics",
    "DebugStage",
    "DebugTrace",
    "DeduplicationResult",
    "DefragmentedBundle",
    "DuplicationAssessment",
    "DuplicationVerdict",
    "FixReadiness",
    "FounderDecisionPackage",
    "GitIntrospectionError",
    "ImpactEstimate",
    "ImpactVerification",
    "IndexDefinition",
    "InformationItem",
    "InformationTemperature",
    "InformationTier",
    "LegalPackage",
    "ProgramCompletionClaim",
    "ProgramStatus",
    "RecoveryCheckpoint",
    "RemoteSyncState",
    "ReopenCandidate",
    "ResearchPackage",
    "RetrievalEffectiveness",
    "RetrievalEvent",
    "StrategyEvaluation",
    "TierTransition",
    "WorkItem",
    "assess_backup_risk",
    "assess_communication_necessity",
    "assess_duplication",
    "assess_fix_readiness",
    "assess_program_completion_claim",
    "assess_reopen_candidate",
    "assess_retrieval_effectiveness",
    "blast_radius",
    "build_code_debug_package",
    "build_compatibility_graph",
    "build_containment_query_fragment",
    "build_founder_decision_package",
    "build_legal_package",
    "build_research_package",
    "claims_local_correctness_only",
    "classify_temperature",
    "deduplicate_exact",
    "defragment",
    "derive_provider_economics_signal",
    "detect_stale_index",
    "estimate_impact",
    "evaluate_strategy_change",
    "filter_known_active_work",
    "find_cross_investigation_reopen_candidates",
    "fragment",
    "get_ahead_behind",
    "get_current_branch",
    "get_dirty_files",
    "get_local_head",
    "get_remote_tracking_sha",
    "is_assignable",
    "latest_communication_for_topic",
    "list_communications_for_topic",
    "parse_handoff_markdown",
    "record_communication",
    "record_stage",
    "score_relevance",
    "select_assignable_agents",
    "select_regression_scope",
    "snapshot_remote_sync_state",
    "transition_tier",
    "trigger_cross_investigation_reopen",
    "validate_checkpoint_reconstructability",
    "validate_graph_provenance_payload",
    "verify_impact",
]
