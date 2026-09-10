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
from app.resource_intelligence.decision import propose_resource_action
from app.resource_intelligence.efficiency_profile import (
    MIN_SAMPLE_SIZE_FOR_ESTABLISHED,
    agent_efficiency_profile,
    is_provisional,
)
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
    "CHECKPOINT_MARKER",
    "CHECKPOINT_NOTE_TYPE",
    "MIN_SAMPLE_SIZE_FOR_ESTABLISHED",
    "AgentSessionCheckpoint",
    "ContextLifecycleAction",
    "MetricEnvelope",
    "ResourceActionRecommendation",
    "ResourceIntelligenceError",
    "agent_efficiency_profile",
    "checkpoint_from_dict",
    "checkpoint_to_dict",
    "context_utilization",
    "cost_for_assignment",
    "cost_per_accepted_commit",
    "estimated_time_to_context_limit",
    "idle_productive_blocked_time",
    "is_provisional",
    "list_telemetry_samples",
    "load_agent_session_checkpoint",
    "next_best_resource_allocation",
    "populate_agent_outcome_cost_fields",
    "propose_resource_action",
    "record_telemetry_sample",
    "save_agent_session_checkpoint",
    "tokens_for_assignment",
    "unknown_metric",
]
