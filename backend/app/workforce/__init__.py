"""MainAI Internal Workforce Foundation — Stage T contracts over existing coordination/security.

Reuse (do not duplicate):
  - app.agent_coordination (reachability registry + repo assignments)
  - app.execution_envelopes / app.provider_spend (real authority + spend)
  - app.egress_policy / provider_disclosure_events (egress ledger)
  - app.intelligence_governance (evidence)
  - app.capability_reality (capability status)

This package owns organizational identity, delegation broker contracts, task-scoped
assignment envelopes, context minimization, performance rollups, and selection scoring.
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
from app.workforce.injection import looks_like_prompt_injection, scrub_authority_mutations
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
from app.workforce.types import (
    AGENT_LIFECYCLE_STATUSES,
    FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS,
    INVARIANTS,
    KNOWN_AGENT_TYPES,
    KNOWN_TRUST_ZONES,
    VERIFICATION_STATUSES,
)

__all__ = [
    "AGENT_LIFECYCLE_STATUSES",
    "AgentNotSelectableError",
    "AuthorityEnvelopeError",
    "CandidateScore",
    "ContextPackagingError",
    "DelegationBrokerError",
    "FORBIDDEN_EXTERNAL_DISCLOSURE_KINDS",
    "INVARIANTS",
    "KNOWN_AGENT_TYPES",
    "KNOWN_TRUST_ZONES",
    "PerformanceLedgerError",
    "SelectorError",
    "TaskScopedAuthority",
    "VERIFICATION_STATUSES",
    "VerificationError",
    "WorkforceRegistryError",
    "assert_agent_selectable",
    "assignment_authority_is_live",
    "cancel_assignment",
    "classify_trust_zone",
    "create_context_package",
    "disable_workforce_agent",
    "form_team",
    "get_or_create_rollup",
    "get_workforce_agent",
    "get_workforce_agent_by_key",
    "ingest_untrusted_result",
    "list_workforce_agents",
    "looks_like_prompt_injection",
    "mark_verification",
    "minimize_for_trust_zone",
    "organization_snapshot",
    "path_allowed",
    "record_job_attempt",
    "record_verified_outcome",
    "register_workforce_agent",
    "require_live_assignment_authority",
    "resolve_delegation",
    "retire_workforce_agent",
    "revoke_assignment_authority",
    "score_candidates",
    "scrub_authority_mutations",
    "select_best_candidate",
    "submit_delegation_request",
    "tool_class_allowed",
    "verified_success_rate",
]
