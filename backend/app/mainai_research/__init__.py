"""MainAI Research, Truth & Advisory Intelligence -- deep investigation + evidence lifecycle +
multi-specialist review + recursive falsification + provider economics + book-grade provenance.

See docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the full architecture
decision. Composes with `app.mainai_vision` (evidence quality axis), `app.resource_intelligence`
(cost/quota signals), `app.mainai_executive` -- never duplicates their own state. One additive
migration (durable, owner-scoped, RLS-forced research ledger; confidence-history and
reopen-events are append-only). RESEARCH != AUTHORITY throughout."""

from app.mainai_research.adapters import (
    ContinuousSupervisionAdapter,
    DevDirectorAdapter,
    PersonalRecallAdapter,
    V1ReadinessAdapter,
    resource_intelligence_cost_signal,
    vision_completion_snapshot,
)
from app.mainai_research.book_provenance import (
    ClaimLineage,
    ResearchCouncilOutput,
    synthesize_research_council_output,
    trace_claim_lineage,
)
from app.mainai_research.causal_reasoning import REQUIRED_TESTS, CausalAssessment, CausalTestOutcome, assess_causal_claim
from app.mainai_research.council import (
    AdversarialCritique,
    CouncilSynthesis,
    ReviewVerdict,
    SpecialistReview,
    adversarial_critique,
    synthesize_council_review,
)
from app.mainai_research.epistemic_caution import CautionAction, CautionAssessment, assess_epistemic_caution
from app.mainai_research.falsification import (
    FalsificationRoundResult,
    failure_to_find_counterevidence_is_not_proof,
    run_falsification_round,
)
from app.mainai_research.investigation_graph import InvestigationGraph, build_investigation_graph
from app.mainai_research.knowledge_ingestion import (
    KNOWLEDGE_ITEM_TRANSITIONS,
    InvalidKnowledgeTransitionError,
    KnowledgeIngestionError,
    TerminalKnowledgeStateError,
    ingest_knowledge_item,
    transition_knowledge_item,
)
from app.mainai_research.procurement_review import (
    ProcurementDecision,
    UserHarmReviewFinding,
    UserHarmReviewResult,
    review_procurement_recommendation,
    review_user_facing_wording,
)
from app.mainai_research.provider_economics import (
    ProviderEconomicsAssessment,
    ProviderEconomicsSignal,
    compare_total_verified_outcome_cost,
    recommend_provider_action,
)
from app.mainai_research.statistics_integrity import (
    MetricVsSystemAssessment,
    RelativeRiskAssessment,
    RelativeRiskClaim,
    SourceCollapseAssessment,
    assess_metric_vs_system_quality,
    assess_relative_risk_claim,
    assess_source_collapse,
)
from app.mainai_research.types import (
    Actor,
    CausalTest,
    EvidenceLifecycleStatus,
    EvidenceRole,
    HypothesisStatus,
    InvestigationStatus,
    KnowledgeItem,
    KnowledgeItemState,
    MoneyFlow,
    ProviderRecommendation,
    Relationship,
    ResearchError,
    SpecialistRole,
    TimelineEvent,
)
from app.mainai_research.words_vs_actions import StatementActionPair, WordsActionsAssessment, assess_words_vs_actions

__all__ = [
    "REQUIRED_TESTS",
    "KNOWLEDGE_ITEM_TRANSITIONS",
    "Actor",
    "AdversarialCritique",
    "CausalAssessment",
    "CausalTest",
    "CausalTestOutcome",
    "CautionAction",
    "CautionAssessment",
    "ClaimLineage",
    "ContinuousSupervisionAdapter",
    "CouncilSynthesis",
    "DevDirectorAdapter",
    "EvidenceLifecycleStatus",
    "EvidenceRole",
    "FalsificationRoundResult",
    "HypothesisStatus",
    "InvalidKnowledgeTransitionError",
    "InvestigationGraph",
    "InvestigationStatus",
    "KnowledgeIngestionError",
    "KnowledgeItem",
    "KnowledgeItemState",
    "MetricVsSystemAssessment",
    "MoneyFlow",
    "PersonalRecallAdapter",
    "ProcurementDecision",
    "ProviderEconomicsAssessment",
    "ProviderEconomicsSignal",
    "ProviderRecommendation",
    "Relationship",
    "RelativeRiskAssessment",
    "RelativeRiskClaim",
    "ResearchCouncilOutput",
    "ResearchError",
    "ReviewVerdict",
    "SourceCollapseAssessment",
    "SpecialistReview",
    "SpecialistRole",
    "StatementActionPair",
    "TerminalKnowledgeStateError",
    "TimelineEvent",
    "UserHarmReviewFinding",
    "UserHarmReviewResult",
    "V1ReadinessAdapter",
    "WordsActionsAssessment",
    "adversarial_critique",
    "assess_causal_claim",
    "assess_epistemic_caution",
    "assess_metric_vs_system_quality",
    "assess_relative_risk_claim",
    "assess_source_collapse",
    "assess_words_vs_actions",
    "build_investigation_graph",
    "compare_total_verified_outcome_cost",
    "failure_to_find_counterevidence_is_not_proof",
    "ingest_knowledge_item",
    "recommend_provider_action",
    "resource_intelligence_cost_signal",
    "review_procurement_recommendation",
    "review_user_facing_wording",
    "run_falsification_round",
    "synthesize_council_review",
    "synthesize_research_council_output",
    "trace_claim_lineage",
    "transition_knowledge_item",
    "vision_completion_snapshot",
]
