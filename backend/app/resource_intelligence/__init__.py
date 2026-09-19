"""MainAI Resource Intelligence + Context Lifecycle + Cost/Quota + Agent Efficiency -- Part 1.

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md for the full architecture
decision. This package composes heavily with `app.agent_coordination`, `app.provider_spend`,
and `app.founder_memory` -- it never duplicates their own tables/ledgers/registries, and never
calls any of their real authority-granting/mutating functions (`create_work_assignment()`,
`authorize_execution_scope()`, `reserve_provider_spend_call()`/`settle_provider_spend_call()`,
...). Read-only against all of them except this package's own new
`agent_resource_telemetry_samples` table and the checkpoint-via-founder_memory mechanism
(which IS meant to write, following `app.mainai_executive.continuity`'s own exact pattern)."""

from app.resource_intelligence.cost_bridge import (
    cost_for_assignment,
    cost_per_accepted_commit,
    populate_agent_outcome_cost_fields,
    tokens_for_assignment,
)
from app.resource_intelligence.cost_projection import (
    compact_cost_estimate,
    estimated_cost_to_finish,
    handoff_cost_estimate,
    reset_session_cost_estimate,
)
from app.resource_intelligence.decision import propose_resource_action
from app.resource_intelligence.efficiency_profile import (
    MIN_SAMPLE_SIZE_FOR_ESTABLISHED,
    agent_efficiency_profile,
    is_provisional,
)
from app.resource_intelligence.founder_attention import (
    ACTION_FOUNDER_ATTENTION,
    FounderAttentionLevel,
    attention_escalation,
    founder_attention_level,
)
from app.resource_intelligence.quota import provider_quota_remaining, quota_critical
from app.resource_intelligence.scheduler import next_best_resource_allocation
from app.resource_intelligence.session_checkpoint import (
    CHECKPOINT_MARKER,
    CHECKPOINT_NOTE_TYPE,
    AgentSessionCheckpoint,
    checkpoint_from_dict,
    checkpoint_to_dict,
    load_agent_session_checkpoint,
    save_agent_session_checkpoint,
)
from app.resource_intelligence.supervision_compat import (
    TranslationResult,
    from_supervision_telemetry_row,
    supervision_cost_fields,
)
from app.resource_intelligence.telemetry import (
    context_utilization,
    estimated_time_to_context_limit,
    idle_productive_blocked_time,
    list_telemetry_samples,
    record_telemetry_sample,
)
from app.resource_intelligence.types import (
    ContextLifecycleAction,
    MetricEnvelope,
    ResourceActionRecommendation,
    ResourceIntelligenceError,
    unknown_metric,
)

__all__ = [
    "ACTION_FOUNDER_ATTENTION",
    "CHECKPOINT_MARKER",
    "CHECKPOINT_NOTE_TYPE",
    "MIN_SAMPLE_SIZE_FOR_ESTABLISHED",
    "AgentSessionCheckpoint",
    "ContextLifecycleAction",
    "FounderAttentionLevel",
    "MetricEnvelope",
    "ResourceActionRecommendation",
    "ResourceIntelligenceError",
    "TranslationResult",
    "agent_efficiency_profile",
    "attention_escalation",
    "checkpoint_from_dict",
    "checkpoint_to_dict",
    "compact_cost_estimate",
    "context_utilization",
    "cost_for_assignment",
    "cost_per_accepted_commit",
    "estimated_cost_to_finish",
    "estimated_time_to_context_limit",
    "founder_attention_level",
    "from_supervision_telemetry_row",
    "handoff_cost_estimate",
    "idle_productive_blocked_time",
    "is_provisional",
    "list_telemetry_samples",
    "load_agent_session_checkpoint",
    "next_best_resource_allocation",
    "populate_agent_outcome_cost_fields",
    "propose_resource_action",
    "provider_quota_remaining",
    "quota_critical",
    "record_telemetry_sample",
    "reset_session_cost_estimate",
    "save_agent_session_checkpoint",
    "supervision_cost_fields",
    "tokens_for_assignment",
    "unknown_metric",
]
