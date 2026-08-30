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
    readiness = evaluate_startup_readiness(claude_reviews_satisfied=None)
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
    }


def assert_provider_invoke_disabled() -> None:
    if PROVIDER_INVOKE_ENABLED:
        raise RuntimeError("PROVIDER_INVOKE_ENABLED must remain False until post-Claude enablement")
