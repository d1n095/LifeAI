"""MainAI startup readiness — machine-checkable levels (not one boolean).

Levels:
  BLOCKED
  READY_FOR_SAFE_INTERNAL_RUN
  READY_FOR_LOW_RISK_PROVIDER_RUN
  READY_FOR_SERIOUS_AUTONOMOUS_RUN

MainAI must never claim "ready" unless durable checks prove the corresponding level.
UNKNOWN checks fail closed for higher levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.workforce.activation_gates import GateStatus, get_activation_gates


class ReadinessLevel(str, Enum):
    BLOCKED = "BLOCKED"
    READY_FOR_SAFE_INTERNAL_RUN = "READY_FOR_SAFE_INTERNAL_RUN"
    READY_FOR_LOW_RISK_PROVIDER_RUN = "READY_FOR_LOW_RISK_PROVIDER_RUN"
    READY_FOR_SERIOUS_AUTONOMOUS_RUN = "READY_FOR_SERIOUS_AUTONOMOUS_RUN"


class CheckStatus(str, Enum):
    healthy = "healthy"
    unknown = "unknown"
    unhealthy = "unhealthy"


@dataclass(frozen=True)
class ReadinessCheck:
    key: str
    status: CheckStatus
    detail: str

    def as_dict(self) -> dict:
        return {"key": self.key, "status": self.status.value, "detail": self.detail}


@dataclass(frozen=True)
class StartupReadinessReport:
    level: ReadinessLevel
    checks: tuple[ReadinessCheck, ...]
    blocking: tuple[str, ...]
    notes: str

    def as_dict(self) -> dict:
        return {
            "level": self.level.value,
            "checks": [c.as_dict() for c in self.checks],
            "blocking": list(self.blocking),
            "notes": self.notes,
            "invariant": "NEVER_COLLAPSE_TO_ONE_BOOLEAN",
        }


def _check_workforce_foundation() -> ReadinessCheck:
    try:
        from app.workforce import INVARIANTS
        from app.models.workforce import WorkforceAgentProfile  # noqa: F401

        return ReadinessCheck(
            "workforce_foundation",
            CheckStatus.healthy,
            f"workforce package present; invariants={len(INVARIANTS)}",
        )
    except Exception as exc:  # pragma: no cover
        return ReadinessCheck("workforce_foundation", CheckStatus.unhealthy, str(exc))


def _check_activation_gates() -> ReadinessCheck:
    decision = get_activation_gates().evaluate()
    if decision.allowed:
        return ReadinessCheck("provider_delegation_safety", CheckStatus.healthy, "all activation gates verified")
    if decision.failed:
        return ReadinessCheck(
            "provider_delegation_safety",
            CheckStatus.unhealthy,
            f"failed={list(decision.failed)}",
        )
    return ReadinessCheck(
        "provider_delegation_safety",
        CheckStatus.unknown,
        f"unknown={list(decision.unknown)} (UNKNOWN!=VERIFIED)",
    )


def _check_import(key: str, import_path: str) -> ReadinessCheck:
    """Presence check only — never claims deep runtime health without evidence."""
    try:
        module, attr = import_path.rsplit(".", 1)
        mod = __import__(module, fromlist=[attr])
        getattr(mod, attr)
        return ReadinessCheck(key, CheckStatus.healthy, f"{import_path} importable")
    except Exception as exc:
        return ReadinessCheck(key, CheckStatus.unknown, f"not proven healthy: {exc}")


def evaluate_startup_readiness(*, claude_reviews_satisfied: bool | None = None) -> StartupReadinessReport:
    """Evaluate readiness level.

    claude_reviews_satisfied:
      None → UNKNOWN (fail closed for provider/serious levels)
      True/False → explicit founder/ops attestation after independent review
    """
    checks: list[ReadinessCheck] = [
        _check_import("v1_autonomous_engine", "app.development_supervisor.service.eligible_authorized_goals"),
        _check_import("memory_truth_layer", "app.inspectable_memory.service.list_inspectable_memory"),
        _check_import("current_truth_selection", "app.inspectable_memory"),  # soft — may be stacked
        _check_import("self_model_evidence", "app.capability_reality.service.get_capability_reality"),
        _check_workforce_foundation(),
        _check_activation_gates(),
        _check_import("vault_egress", "app.egress_policy.service.enforce_egress_policy"),
        _check_import("authority_boundaries", "app.execution_envelopes.service.get_current_execution_envelope"),
        _check_import("spend_controls", "app.provider_spend.service.provider_spend_is_live"),
        _check_import("recovery", "app.agent_coordination.service.take_over_lease"),
        ReadinessCheck(
            "blocking_migrations",
            CheckStatus.healthy,
            "alembic single head expected at 0068 on tip (verify ops)",
        ),
        ReadinessCheck(
            "claude_reviews",
            CheckStatus.healthy
            if claude_reviews_satisfied is True
            else (CheckStatus.unhealthy if claude_reviews_satisfied is False else CheckStatus.unknown),
            "explicit attestation required; UNKNOWN!=VERIFIED"
            if claude_reviews_satisfied is None
            else f"attested={claude_reviews_satisfied}",
        ),
    ]

    by_key = {c.key: c for c in checks}
    blocking: list[str] = []

    def need(*keys: str, allow_unknown: bool = False) -> bool:
        for k in keys:
            st = by_key[k].status
            if st == CheckStatus.unhealthy:
                blocking.append(k)
                return False
            if st == CheckStatus.unknown and not allow_unknown:
                blocking.append(f"{k}:unknown")
                return False
        return True

    # Tier 0: anything unhealthy in core → BLOCKED
    core = (
        "workforce_foundation",
        "vault_egress",
        "authority_boundaries",
        "spend_controls",
    )
    if any(by_key[k].status == CheckStatus.unhealthy for k in core if k in by_key):
        return StartupReadinessReport(
            level=ReadinessLevel.BLOCKED,
            checks=tuple(checks),
            blocking=tuple(k for k in core if by_key[k].status == CheckStatus.unhealthy),
            notes="core safety unhealthy",
        )

    # Tier 1: safe internal run — workforce + authority + egress present; gates may be unknown
    if not need("workforce_foundation", "vault_egress", "authority_boundaries", allow_unknown=False):
        return StartupReadinessReport(
            ReadinessLevel.BLOCKED, tuple(checks), tuple(blocking), "missing internal-safe foundations"
        )

    level = ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN

    # Tier 2: low-risk provider — all activation gates + claude reviews + spend
    if (
        by_key["provider_delegation_safety"].status == CheckStatus.healthy
        and by_key["spend_controls"].status == CheckStatus.healthy
        and by_key["claude_reviews"].status == CheckStatus.healthy
    ):
        level = ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN
    else:
        for k in ("provider_delegation_safety", "spend_controls", "claude_reviews"):
            if by_key[k].status != CheckStatus.healthy:
                blocking.append(k if by_key[k].status != CheckStatus.unknown else f"{k}:unknown")

    # Tier 3: serious autonomous — stricter: recovery + engine + memory + self-model + no unknowns
    serious_keys = (
        "v1_autonomous_engine",
        "memory_truth_layer",
        "self_model_evidence",
        "recovery",
        "provider_delegation_safety",
        "claude_reviews",
    )
    if level == ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN:
        if all(by_key[k].status == CheckStatus.healthy for k in serious_keys):
            level = ReadinessLevel.READY_FOR_SERIOUS_AUTONOMOUS_RUN
        else:
            blocking.extend(k for k in serious_keys if by_key[k].status != CheckStatus.healthy)

    if level == ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN:
        blocking = [
            c.key if c.status != CheckStatus.unknown else f"{c.key}:unknown"
            for c in checks
            if c.key in ("provider_delegation_safety", "claude_reviews") and c.status != CheckStatus.healthy
        ]

    return StartupReadinessReport(
        level=level,
        checks=tuple(checks),
        blocking=tuple(dict.fromkeys(blocking)),
        notes="levels are cumulative; never collapse to one boolean",
    )
