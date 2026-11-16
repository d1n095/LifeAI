"""Guardian / Trust Kernel (MainAI V2, Stage V2-B).

Standalone, isolated, NOT imported by any production runtime path (no app.main, no
app.workforce import from this package). See docs/mainai_v2/MAINAI_V2_GUARDIAN_TRUST_KERNEL.md.
"""

from app.guardian.policy import GuardianPolicy, PolicyRejected, default_policy, sign_policy, verify_policy_signature
from app.guardian.service import (
    GuardianState,
    apply_new_policy,
    clear_containment,
    evaluate_authority_ceiling_request,
    evaluate_bounded_action,
    evaluate_containment_request,
    evaluate_recovery_trigger,
    from_snapshot,
    guardian_verify_owner_for_recovery,
    new_guardian_state,
    set_device_trust,
    set_integrity_state,
    to_snapshot,
    verify_receipt_chain_intact,
)
from app.guardian.types import (
    AuthorityCeiling,
    AuthorityCeilingRequest,
    ContainmentReceipt,
    ContainmentRequest,
    ContainmentScope,
    DeviceTrustState,
    GuardianAction,
    GuardianDecision,
    GuardianReason,
    IntegrityState,
    RecoveryTrigger,
    SovereignIdentityAssertion,
)

__all__ = [
    "GuardianPolicy",
    "PolicyRejected",
    "default_policy",
    "sign_policy",
    "verify_policy_signature",
    "GuardianState",
    "apply_new_policy",
    "clear_containment",
    "evaluate_authority_ceiling_request",
    "evaluate_bounded_action",
    "evaluate_containment_request",
    "evaluate_recovery_trigger",
    "from_snapshot",
    "guardian_verify_owner_for_recovery",
    "new_guardian_state",
    "set_device_trust",
    "set_integrity_state",
    "to_snapshot",
    "verify_receipt_chain_intact",
    "AuthorityCeiling",
    "AuthorityCeilingRequest",
    "ContainmentReceipt",
    "ContainmentRequest",
    "ContainmentScope",
    "DeviceTrustState",
    "GuardianAction",
    "GuardianDecision",
    "GuardianReason",
    "IntegrityState",
    "RecoveryTrigger",
    "SovereignIdentityAssertion",
]
