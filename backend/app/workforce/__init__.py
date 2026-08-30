"""MainAI Internal Workforce Foundation — Stage T contracts over existing coordination/security.

Reuse (do not duplicate):
  - app.agent_coordination (reachability registry + repo assignments)
  - app.execution_envelopes / app.provider_spend (real authority + spend)
  - app.egress_policy / provider_disclosure_events (egress ledger)
  - app.intelligence_governance (evidence)
  - app.capability_reality (capability status)

This package owns organizational identity, delegation broker contracts, task-scoped
assignment envelopes, context minimization, performance rollups, selection scoring,
failure/takeover, verification policy, cost budgets, hiring lifecycle, and teams.
"""

from app.workforce.authority import (
    AuthorityEnvelopeError,
    TaskScopedAuthority,
    assignment_authority_is_live,
    path_allowed,
    require_live_assignment_authority,
    revoke_assignment_authority,
    tool_class_allowed,
)
from app.workforce.broker import (
    DelegationBrokerError,
    VerificationError,
    cancel_assignment,
    form_team,
    ingest_untrusted_result,
    mark_verification,
    resolve_delegation,
    submit_delegation_request,
)
from app.workforce.context import (
    ContextPackagingError,
    classify_trust_zone,
    create_context_package,
    minimize_for_trust_zone,
)
from app.workforce.cost import (
    CostGovernanceError,
    assert_scopes_allow_spend,
    budget_is_live,
    budget_remaining,
    release_budget_reservation,
    reserve_against_budget,
    set_cost_budget,
    settle_budget_reservation,
)
from app.workforce.failure import (
    FailureTakeoverError,
    alternate_agent_takeover,
    can_safely_retry,
    latest_checkpoint,
    mark_failure,
    record_checkpoint,
    resume_after_restart,
    safe_retry_same_agent,
)
from app.workforce.injection import (
    fail_closed_on_secret_request,
    looks_like_prompt_injection,
    refuse_role_or_tool_self_upgrade,
    scrub_authority_mutations,
)
from app.workforce.lifecycle import (
    LifecycleError,
    advance_lifecycle,
    detect_need_and_create_candidate,
    record_improvement,
    run_hiring_pipeline,
)
from app.workforce.org_view import organization_snapshot
from app.workforce.performance import (
    PerformanceLedgerError,
    get_or_create_rollup,
    record_job_attempt,
    record_verified_outcome,
    verified_success_rate,
)
from app.workforce.registry import (
    AgentNotSelectableError,
    WorkforceRegistryError,
    assert_agent_selectable,
    disable_workforce_agent,
    get_workforce_agent,
    get_workforce_agent_by_key,
    list_workforce_agents,
    register_workforce_agent,
    retire_workforce_agent,
)
from app.workforce.selector import CandidateScore, SelectorError, score_candidates, select_best_candidate
from app.workforce.teams import (
    KNOWN_TEAM_PATTERNS,
    assert_no_automatic_cross_context,
    form_pattern_team,
    package_context_per_member,
)
from app.workforce.types import (
    AGENT_LIFECYCLE_STATUSES,
    FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS,
    INVARIANTS,
    KNOWN_AGENT_TYPES,
    KNOWN_TRUST_ZONES,
    VERIFICATION_STATUSES,
)
from app.workforce.verification import VerificationPolicy, apply_verification_decision, policy_for_risk
from app.workforce.vertical_slice import LowRiskSliceResult, run_low_risk_classification_slice

__all__ = [
    "AGENT_LIFECYCLE_STATUSES",
    "AgentNotSelectableError",
    "AuthorityEnvelopeError",
    "CandidateScore",
    "ContextPackagingError",
    "CostGovernanceError",
    "DelegationBrokerError",
    "FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS",
    "FailureTakeoverError",
    "INVARIANTS",
    "KNOWN_AGENT_TYPES",
    "KNOWN_TEAM_PATTERNS",
    "KNOWN_TRUST_ZONES",
    "LifecycleError",
    "LowRiskSliceResult",
    "PerformanceLedgerError",
    "SelectorError",
    "TaskScopedAuthority",
    "VERIFICATION_STATUSES",
    "VerificationError",
    "VerificationPolicy",
    "WorkforceRegistryError",
    "advance_lifecycle",
    "alternate_agent_takeover",
    "apply_verification_decision",
    "assert_agent_selectable",
    "assert_no_automatic_cross_context",
    "assert_scopes_allow_spend",
    "assignment_authority_is_live",
    "budget_is_live",
    "budget_remaining",
    "can_safely_retry",
    "cancel_assignment",
    "classify_trust_zone",
    "create_context_package",
    "detect_need_and_create_candidate",
    "disable_workforce_agent",
    "fail_closed_on_secret_request",
    "form_pattern_team",
    "form_team",
    "get_or_create_rollup",
    "get_workforce_agent",
    "get_workforce_agent_by_key",
    "ingest_untrusted_result",
    "latest_checkpoint",
    "list_workforce_agents",
    "looks_like_prompt_injection",
    "mark_failure",
    "mark_verification",
    "minimize_for_trust_zone",
    "organization_snapshot",
    "package_context_per_member",
    "path_allowed",
    "policy_for_risk",
    "record_checkpoint",
    "record_improvement",
    "record_job_attempt",
    "record_verified_outcome",
    "refuse_role_or_tool_self_upgrade",
    "register_workforce_agent",
    "release_budget_reservation",
    "require_live_assignment_authority",
    "reserve_against_budget",
    "resolve_delegation",
    "resume_after_restart",
    "retire_workforce_agent",
    "revoke_assignment_authority",
    "run_hiring_pipeline",
    "run_low_risk_classification_slice",
    "safe_retry_same_agent",
    "score_candidates",
    "scrub_authority_mutations",
    "select_best_candidate",
    "set_cost_budget",
    "settle_budget_reservation",
    "submit_delegation_request",
    "tool_class_allowed",
    "verified_success_rate",
]
