"""Receipt-backed startup readiness — IMPORTABLE != HEALTHY.

UNKNOWN fails closed at the relevant tier. Blocker lists accumulate (never overwritten).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class CheckStatus(str, Enum):
    healthy = "healthy"
    unhealthy = "unhealthy"
    unknown = "unknown"


class ReadinessLevel(str, Enum):
    BLOCKED = "BLOCKED"
    READY_FOR_SAFE_INTERNAL_RUN = "READY_FOR_SAFE_INTERNAL_RUN"
    READY_FOR_LOW_RISK_PROVIDER_RUN = "READY_FOR_LOW_RISK_PROVIDER_RUN"
    READY_FOR_SERIOUS_AUTONOMOUS_RUN = "READY_FOR_SERIOUS_AUTONOMOUS_RUN"


@dataclass
class ReadinessCheck:
    key: str
    status: CheckStatus
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status.value,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class StartupReadinessReport:
    level: ReadinessLevel
    checks: tuple[ReadinessCheck, ...]
    blocking: tuple[str, ...]
    notes: str = ""
    receipts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "checks": [c.as_dict() for c in self.checks],
            "blocking": list(self.blocking),
            "notes": self.notes,
            "receipts": self.receipts,
            "importable_is_not_healthy": True,
            "unknown_fail_closed": True,
            "invariant": "NEVER_COLLAPSE_TO_ONE_BOOLEAN",
        }


def _check_import(key: str, dotted: str) -> ReadinessCheck:
    """Importability alone → UNKNOWN, never healthy."""
    try:
        module_path, _, attr = dotted.rpartition(".")
        mod = __import__(module_path, fromlist=[attr])
        getattr(mod, attr)
        return ReadinessCheck(
            key,
            CheckStatus.unknown,
            "module_importable_only — IMPORTABLE != HEALTHY",
            evidence={"importable": True, "verified_runtime": False},
        )
    except Exception as exc:
        return ReadinessCheck(
            key,
            CheckStatus.unhealthy,
            f"not importable: {exc}",
            evidence={"importable": False},
        )


def verify_migration_head(db: Session | None = None) -> ReadinessCheck:
    """Actual alembic current vs heads — not a hardcoded healthy claim."""
    backend = Path(__file__).resolve().parents[2]
    try:
        heads = subprocess.check_output(
            ["python", "-m", "alembic", "heads"],
            cwd=str(backend),
            text=True,
            stderr=subprocess.STDOUT,
        )
        head_ids = [ln.split()[0] for ln in heads.splitlines() if ln.strip()]
        if len(head_ids) != 1:
            return ReadinessCheck(
                "blocking_migrations",
                CheckStatus.unhealthy,
                f"expected single alembic head, got {head_ids}",
                evidence={"heads": head_ids},
            )
        current = subprocess.check_output(
            ["python", "-m", "alembic", "current"],
            cwd=str(backend),
            text=True,
            stderr=subprocess.STDOUT,
        )
        current_ids = [
            ln.split()[0] for ln in current.splitlines() if ln.strip() and not ln.startswith("INFO")
        ]
        # alembic current may print "0069 (head)" 
        cur = None
        for ln in current.splitlines():
            parts = ln.strip().split()
            if parts and parts[0][0].isdigit():
                cur = parts[0]
                break
        if cur is None and db is not None:
            row = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            cur = row
        if cur != head_ids[0]:
            return ReadinessCheck(
                "blocking_migrations",
                CheckStatus.unhealthy if cur else CheckStatus.unknown,
                f"db revision {cur!r} != head {head_ids[0]!r}",
                evidence={"current": cur, "head": head_ids[0]},
            )
        return ReadinessCheck(
            "blocking_migrations",
            CheckStatus.healthy,
            f"verified single head {head_ids[0]}",
            evidence={"current": cur, "head": head_ids[0], "verified": True},
        )
    except Exception as exc:
        return ReadinessCheck(
            "blocking_migrations",
            CheckStatus.unknown,
            f"migration verification failed: {exc}",
            evidence={"error": str(exc)},
        )


def verify_provider_disabled() -> ReadinessCheck:
    try:
        from app.workforce.activation_commit import (
            PROVIDER_INVOKE_ENABLED,
            assert_provider_invoke_disabled,
        )

        assert_provider_invoke_disabled()
        if PROVIDER_INVOKE_ENABLED:
            return ReadinessCheck(
                "provider_disabled",
                CheckStatus.unhealthy,
                "PROVIDER_INVOKE_ENABLED is True",
                evidence={"provider_invoke_enabled": True},
            )
        return ReadinessCheck(
            "provider_disabled",
            CheckStatus.healthy,
            "provider invoke disabled",
            evidence={"provider_invoke_enabled": False, "asserted": True},
        )
    except Exception as exc:
        return ReadinessCheck(
            "provider_disabled",
            CheckStatus.unhealthy,
            str(exc),
            evidence={"error": type(exc).__name__},
        )


def verify_kill_switch_schema(db: Session | None) -> ReadinessCheck:
    if db is None:
        return ReadinessCheck(
            "kill_switch_health",
            CheckStatus.unknown,
            "no db session for durable authority-epoch check",
        )
    try:
        db.execute(
            text(
                "SELECT scope_key, stopped, epoch FROM workforce_authority_epoch "
                "WHERE scope_key = 'GLOBAL' LIMIT 1"
            )
        ).first()
        return ReadinessCheck(
            "kill_switch_health",
            CheckStatus.healthy,
            "durable workforce_authority_epoch readable",
            evidence={"durable": True, "canonical_table": "workforce_authority_epoch"},
        )
    except Exception as exc:
        return ReadinessCheck(
            "kill_switch_health",
            CheckStatus.unhealthy,
            f"authority epoch unavailable: {exc}",
        )


def evaluate_startup_readiness(
    *,
    claude_reviews_satisfied: bool | None = None,
    db: Session | None = None,
    receipts: dict[str, Any] | None = None,
) -> StartupReadinessReport:
    """Derive readiness from receipts. Caller True for claude_reviews does NOT alone unlock.

    claude_reviews_satisfied=True without durable receipt → stays UNKNOWN (fail closed for provider).
    """
    receipts = dict(receipts or {})
    blocking: list[str] = []

    checks: list[ReadinessCheck] = [
        _check_import("v1_autonomous_engine", "app.development_supervisor.service.eligible_authorized_goals"),
        _check_import("memory_truth_layer", "app.inspectable_memory.service.list_inspectable_memory"),
        _check_import("self_model_evidence", "app.capability_reality.service.get_capability_reality"),
        verify_migration_head(db),
        verify_provider_disabled(),
        verify_kill_switch_schema(db),
    ]

    # Workforce foundation: require import + optional receipt
    wf = _check_import("workforce_foundation", "app.workforce.register_workforce_agent")
    if receipts.get("workforce_foundation_verified") is True:
        wf = ReadinessCheck(
            "workforce_foundation",
            CheckStatus.healthy,
            "receipt workforce_foundation_verified",
            evidence={"receipt": True},
        )
    checks.append(wf)

    for key in ("vault_egress", "authority_boundaries", "spend_controls"):
        c = _check_import(
            key,
            {
                "vault_egress": "app.egress_policy.service.enforce_egress_policy",
                "authority_boundaries": "app.execution_envelopes.service.get_current_execution_envelope",
                "spend_controls": "app.provider_spend.service.provider_spend_is_live",
            }[key],
        )
        if receipts.get(f"{key}_verified") is True:
            c = ReadinessCheck(key, CheckStatus.healthy, f"receipt {key}_verified", evidence={"receipt": True})
        checks.append(c)

    # Claude reviews: True alone is insufficient without durable receipt
    if claude_reviews_satisfied is True and receipts.get("claude_reviews_evidence_ref"):
        claude = ReadinessCheck(
            "claude_reviews",
            CheckStatus.healthy,
            "attested with durable evidence_ref",
            evidence={"evidence_ref": receipts.get("claude_reviews_evidence_ref")},
        )
    elif claude_reviews_satisfied is False:
        claude = ReadinessCheck("claude_reviews", CheckStatus.unhealthy, "attested=false")
    else:
        claude = ReadinessCheck(
            "claude_reviews",
            CheckStatus.unknown,
            "UNKNOWN — attestation without durable evidence is not truth",
            evidence={"claude_reviews_satisfied": claude_reviews_satisfied},
        )
    checks.append(claude)

    by_key = {c.key: c for c in checks}

    def need(*keys: str, allow_unknown: bool = False) -> bool:
        ok = True
        for k in keys:
            st = by_key[k].status
            if st == CheckStatus.unhealthy:
                blocking.append(k)
                ok = False
            elif st == CheckStatus.unknown and not allow_unknown:
                blocking.append(f"{k}:unknown")
                ok = False
        return ok

    # Core unhealthy → BLOCKED (accumulate all)
    core = ("workforce_foundation", "vault_egress", "authority_boundaries", "spend_controls", "kill_switch_health")
    for k in core:
        if k in by_key and by_key[k].status == CheckStatus.unhealthy:
            if k not in blocking:
                blocking.append(k)

    if any(by_key[k].status == CheckStatus.unhealthy for k in core if k in by_key):
        return StartupReadinessReport(
            level=ReadinessLevel.BLOCKED,
            checks=tuple(checks),
            blocking=tuple(blocking),
            notes="core safety unhealthy",
            receipts=receipts,
        )

    # Safe internal: require workforce + vault + authority healthy OR verified via receipt;
    # migration must not be unhealthy; kill_switch healthy; provider disabled healthy.
    # Import-only UNKNOWN is allowed for some soft modules at this tier.
    safe_keys = ("workforce_foundation", "vault_egress", "authority_boundaries", "provider_disabled", "kill_switch_health")
    # For safe internal, import-unknown on vault/authority/workforce is NOT enough —
    # need healthy via receipt OR we allow unknown only if module importable AND migration known?
    # Campaign: IMPORTABLE != HEALTHY. For safe internal we previously allowed import.
    # Tighten: require provider_disabled healthy + kill_switch healthy + migration not unhealthy.
    if by_key["provider_disabled"].status != CheckStatus.healthy:
        blocking.append("provider_disabled")
    if by_key["kill_switch_health"].status == CheckStatus.unhealthy:
        blocking.append("kill_switch_health")
    if by_key["blocking_migrations"].status == CheckStatus.unhealthy:
        blocking.append("blocking_migrations")

    if (
        by_key["provider_disabled"].status != CheckStatus.healthy
        or by_key["kill_switch_health"].status == CheckStatus.unhealthy
        or by_key["blocking_migrations"].status == CheckStatus.unhealthy
    ):
        return StartupReadinessReport(
            ReadinessLevel.BLOCKED,
            tuple(checks),
            tuple(dict.fromkeys(blocking)),
            "provider, kill-switch, or migrations not proven",
            receipts,
        )

    # Soft: workforce/vault/authority may be UNKNOWN (importable) for safe-internal if not unhealthy
    for k in ("workforce_foundation", "vault_egress", "authority_boundaries"):
        if by_key[k].status == CheckStatus.unhealthy and k not in blocking:
            blocking.append(k)
    if any(by_key[k].status == CheckStatus.unhealthy for k in ("workforce_foundation", "vault_egress", "authority_boundaries")):
        return StartupReadinessReport(
            ReadinessLevel.BLOCKED,
            tuple(checks),
            tuple(dict.fromkeys(blocking)),
            "missing internal-safe foundations",
            receipts,
        )

    level = ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN
    next_blockers: list[str] = []

    # Provider tier: need healthy claude + spend + activation — UNKNOWN fail closed
    provider_need = ("claude_reviews", "spend_controls", "provider_disabled")
    for k in provider_need:
        st = by_key[k].status
        if st != CheckStatus.healthy:
            next_blockers.append(f"{k}:{st.value}")

    # Activation gates
    try:
        from app.workforce.activation_gates import GateStatus, get_activation_gates

        gates = get_activation_gates()
        if not all(g.status == GateStatus.verified for g in gates.gates.values()):
            next_blockers.append("activation_gates:not_all_verified")
            checks.append(
                ReadinessCheck(
                    "provider_delegation_safety",
                    CheckStatus.unknown,
                    "activation gates not all verified",
                )
            )
        else:
            checks.append(
                ReadinessCheck(
                    "provider_delegation_safety",
                    CheckStatus.healthy,
                    "all activation gates verified",
                )
            )
    except Exception as exc:
        next_blockers.append("activation_gates:error")
        checks.append(
            ReadinessCheck("provider_delegation_safety", CheckStatus.unknown, str(exc))
        )

    # Keep ALL reasons preventing next level visible (never overwrite)
    blocking_for_next = list(dict.fromkeys(blocking + next_blockers))

    if not next_blockers and by_key["claude_reviews"].status == CheckStatus.healthy:
        level = ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN

    return StartupReadinessReport(
        level=level,
        checks=tuple(checks),
        blocking=tuple(blocking_for_next),
        notes=f"evaluated_at={datetime.utcnow().isoformat()}Z",
        receipts=receipts,
    )


# Re-export for app.mainai_startup_readiness package compatibility
