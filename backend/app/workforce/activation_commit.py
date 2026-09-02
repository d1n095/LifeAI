"""Staged activation commit helpers — DISABLED BY DEFAULT.

Do NOT enable real provider invoke here. After Claude verifies gates with evidence,
a separate explicit enablement step may flip PROVIDER_INVOKE_ENABLED only when
ActivationGateSet.evaluate().allowed and readiness == READY_FOR_LOW_RISK_PROVIDER_RUN.
"""

from __future__ import annotations

from app.mainai_startup_readiness import ReadinessLevel, evaluate_startup_readiness
from app.workforce.activation_gates import get_activation_gates


# Hard default — never flip in this prep PR.
PROVIDER_INVOKE_ENABLED: bool = False


def activation_commit_status() -> dict:
    gates = get_activation_gates().evaluate()
    # Wire claude_reviews_satisfied from the SAME ActivationGateSet evaluated above,
    # rather than hardcoding None: True only when every REQUIRED_ACTIVATION_GATES key
    # is VERIFIED (gates.allowed), False if any is explicitly FAILED, else UNKNOWN.
    # Previously this was unconditionally None, which forced evaluate_startup_
    # readiness()'s own "claude_reviews" check to CheckStatus.unknown forever --
    # capping readiness.level at READY_FOR_SAFE_INTERNAL_RUN regardless of actual
    # verified state, so ready_to_enable_after_claude could never become True.
    if gates.failed:
        claude_reviews_satisfied: bool | None = False
        receipts: dict = {}
    elif gates.allowed:
        claude_reviews_satisfied = True
        # IMPORTABLE != HEALTHY / True alone != unlock: attach durable evidence_refs
        # from the verified gates themselves (each VERIFIED gate already required one).
        gate_set = get_activation_gates()
        refs = sorted(
            {
                g.evidence_ref
                for g in gate_set.gates.values()
                if getattr(g, "evidence_ref", None)
            }
        )
        receipts = {
            "claude_reviews_evidence_ref": ",".join(refs) if refs else "gates:all_verified",
        }
    else:
        claude_reviews_satisfied = None
        receipts = {}
    readiness = evaluate_startup_readiness(
        claude_reviews_satisfied=claude_reviews_satisfied,
        receipts=receipts,
    )
    return {
        "provider_invoke_enabled": PROVIDER_INVOKE_ENABLED,
        "gates_allowed": gates.allowed,
        "gates_unknown": list(gates.unknown),
        "gates_failed": list(gates.failed),
        "readiness_level": readiness.level.value,
        "ready_to_enable_after_claude": (
            gates.allowed
            and readiness.level
            in (
                ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN,
                ReadinessLevel.READY_FOR_SERIOUS_AUTONOMOUS_RUN,
            )
            and PROVIDER_INVOKE_ENABLED is False
        ),
        "note": "Enable only after evidence-backed gate verification + dedicated enablement commit",
        "receipts": receipts,
    }


def assert_provider_invoke_disabled() -> None:
    if PROVIDER_INVOKE_ENABLED:
        raise RuntimeError("PROVIDER_INVOKE_ENABLED must remain False until post-Claude enablement")
