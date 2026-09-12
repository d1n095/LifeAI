"""MainAI Cognitive Control Plane -- Vision Compiler + Dynamic Completion Engine + Vision Gap
Generator + Cognitive Loop + Statistics Command Center + Evidence Intelligence + Continuous
Improvement + Founder Program Truth.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the full architecture
decision. This package composes heavily with `app.project_entities`, `app.mainai_executive`,
`app.resource_intelligence`, `app.work_candidates` -- it never duplicates their own tables/
registries, and never calls any of their real authority-granting/mutating functions
(`promote_interpretation_proposal()`, `authorize_work_candidate()`, `transition_status()`, ...).
Read-only against all of them except this package's own use of the EXISTING, unchanged
`record_interpretation_proposal()` (staging only) and `record_founder_memory()` (mind-change
ledger, following `session_checkpoint.py`'s own exact precedent)."""

from app.mainai_vision.adapters import (
    ContinuousSupervisionAdapter,
    DevDirectorAdapter,
    PersonalRecallAdapter,
    V1ReadinessAdapter,
    founder_reasoning_snapshot,
    resource_intelligence_snapshot,
)
from app.mainai_vision.cognitive_loop import CognitiveLoopResult, run_cognitive_cycle
from app.mainai_vision.completion import (
    CompletionDimension,
    CompletionReport,
    NodeCompletionInput,
    assess_program_completion,
    compute_completion,
    compute_node_maturity,
    derive_baseline_maturity,
)
from app.mainai_vision.evidence import (
    ConfoundCheck,
    EvidenceClaim,
    EvidenceState,
    MindChangeRecord,
    RawEvidence,
    classify_claim_state,
    count_independent_sources,
    evaluate_confounds,
)
from app.mainai_vision.founder_truth import founder_program_truth
from app.mainai_vision.gap_generator import GapReport, ImpliedRequirement, persist_gap_proposals, propose_implied_requirements
from app.mainai_vision.improvement import ImprovementCategory, ImprovementProposal, LoopKind, choose_loop, propose_next_improvement
from app.mainai_vision.mind_change import load_mind_change_record, save_mind_change_record
from app.mainai_vision.statistics import ObservationBasis, StatisticsRecord, compile_statistics_registry
from app.mainai_vision.types import (
    MaturityState,
    MetricEnvelope,
    VisionEdge,
    VisionEdgeKind,
    VisionError,
    VisionGraph,
    VisionNode,
    VisionNodeKind,
    unknown_metric,
)
from app.mainai_vision.vision_compiler import compile_vision_graph

__all__ = [
    "CognitiveLoopResult",
    "CompletionDimension",
    "CompletionReport",
    "ConfoundCheck",
    "ContinuousSupervisionAdapter",
    "DevDirectorAdapter",
    "EvidenceClaim",
    "EvidenceState",
    "GapReport",
    "ImpliedRequirement",
    "ImprovementCategory",
    "ImprovementProposal",
    "LoopKind",
    "MaturityState",
    "MetricEnvelope",
    "MindChangeRecord",
    "NodeCompletionInput",
    "ObservationBasis",
    "PersonalRecallAdapter",
    "RawEvidence",
    "StatisticsRecord",
    "V1ReadinessAdapter",
    "VisionEdge",
    "VisionEdgeKind",
    "VisionError",
    "VisionGraph",
    "VisionNode",
    "VisionNodeKind",
    "assess_program_completion",
    "choose_loop",
    "classify_claim_state",
    "compile_statistics_registry",
    "compile_vision_graph",
    "compute_completion",
    "compute_node_maturity",
    "count_independent_sources",
    "derive_baseline_maturity",
    "evaluate_confounds",
    "founder_program_truth",
    "founder_reasoning_snapshot",
    "load_mind_change_record",
    "persist_gap_proposals",
    "propose_implied_requirements",
    "propose_next_improvement",
    "resource_intelligence_snapshot",
    "run_cognitive_cycle",
    "save_mind_change_record",
    "unknown_metric",
]
