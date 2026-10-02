"""Founder sovereignty + family delegation foundation.

The Founder MainAI instance binds to exactly one Founder. Family members receive
only scoped Life capabilities through governed approvals. This package does not
grant merge, deploy, Recall, or database-superuser authority.
"""

from app.mainai_founder_sovereignty.kernel import KERNEL_INVARIANTS
from app.mainai_founder_sovereignty.service import (
    add_family_member,
    apply_founder_policy,
    approval_context,
    authorize_capability,
    authorize_with_copied_token,
    authorize_with_receipt,
    bind_founder_instance,
    decide_approval,
    define_userai_boundary,
    disable_founder_policy,
    inspect_active_policies,
    list_pending_approvals,
    propose_policy,
    refuse_non_founder_policy_source,
    refuse_self_unlock,
    request_family_capability,
    revoke_grant,
    rollback_founder_policy,
    set_workflow_lock,
)
from app.mainai_founder_sovereignty.types import ActorKind, ApprovalMode, PolicyClass, PolicySource, RiskTier

__all__ = [
    "KERNEL_INVARIANTS",
    "ActorKind",
    "ApprovalMode",
    "PolicyClass",
    "PolicySource",
    "RiskTier",
    "add_family_member",
    "apply_founder_policy",
    "approval_context",
    "authorize_capability",
    "authorize_with_copied_token",
    "authorize_with_receipt",
    "bind_founder_instance",
    "decide_approval",
    "define_userai_boundary",
    "disable_founder_policy",
    "inspect_active_policies",
    "list_pending_approvals",
    "propose_policy",
    "refuse_non_founder_policy_source",
    "refuse_self_unlock",
    "request_family_capability",
    "revoke_grant",
    "rollback_founder_policy",
    "set_workflow_lock",
]
