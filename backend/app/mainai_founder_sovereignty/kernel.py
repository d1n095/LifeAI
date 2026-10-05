"""Non-overridable security core.

Founder-policy recovery must not allow AI to become Founder, bypass identity
verification, grant credentials, disable owner isolation, or rewrite history.
"""

from __future__ import annotations

from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.types import PolicyClass, SovereigntyError

KERNEL_INVARIANTS: dict[str, str] = {
    "founder_identity": "MainAI binds to exactly one Founder identity (FOUNDER_USER_ID). AI cannot become Founder.",
    "founder_authentication": "Founder identity verification cannot be bypassed by policy.",
    "owner_isolation": "Owner isolation / RLS cannot be disabled by founder-policy recovery.",
    "audit_immutability": "Historical audit evidence and approval receipts cannot be rewritten.",
    "no_silent_credentials": "Credentials cannot be granted silently through founder policy.",
    "recall_default_off": "Personal Recall stays default-off. This package never activates it.",
    "no_merge_deploy_superuser": "This subsystem does not grant merge, deploy, Recall, or database-superuser authority.",
}


def assert_founder_identity(user_id) -> None:
    """Public sentinel comparison only — never treat this as authentication."""
    if user_id != FOUNDER_USER_ID:
        raise SovereigntyError("not_founder", "FAMILY MEMBER != FOUNDER; ADMIN != FOUNDER; AI != FOUNDER")


def refuse_kernel_mutation(policy_class: PolicyClass, policy_key: str) -> None:
    if policy_class is PolicyClass.KERNEL_SECURITY_INVARIANT or policy_key in KERNEL_INVARIANTS:
        raise SovereigntyError(
            "kernel_immutable",
            "KERNEL_SECURITY_INVARIANT cannot be changed, unlocked, or rolled back by founder policy",
        )


def is_kernel_invariant_key(policy_key: str) -> bool:
    return policy_key in KERNEL_INVARIANTS
