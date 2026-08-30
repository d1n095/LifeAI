"""Explicit provider-activation gate object.

UNKNOWN != VERIFIED.
If any required gate is unknown or failed → FAIL CLOSED.
Never auto-promote from CI; founder/ops must record verification with evidence refs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ProviderActivationBlocked(Exception):
    """Raised when real provider execution is requested but safety gates are unmet."""


class GateStatus(str, Enum):
    unknown = "unknown"
    verified = "verified"
    failed = "failed"


# Stable keys — map to PR/review evidence, not scattered booleans.
REQUIRED_ACTIVATION_GATES: tuple[str, ...] = (
    "pr_218_consequential_confirmation",
    "pr_229_same_collapse_race",
    "pr_213_self_model_evidence",
    "pr_224_current_truth_selection",
    "workforce_context_isolation",
    "workforce_authority_tests",
    "provider_spend_gate",
    "verification_independence",
)

# Human-readable map for inspectability.
GATE_DESCRIPTIONS: dict[str, str] = {
    "pr_218_consequential_confirmation": "Personal-intent consequential confirmation (#218) independently verified",
    "pr_229_same_collapse_race": "SAME-collapse concurrency recovery (#229) independently verified",
    "pr_213_self_model_evidence": "Self-model / capability evidence gates (#213) independently verified",
    "pr_224_current_truth_selection": "Current-truth selection (#224) independently verified",
    "workforce_context_isolation": "Workforce context isolation / disclosure minimization tests green",
    "workforce_authority_tests": "Workforce task-scoped authority / no-widen tests green",
    "provider_spend_gate": "Provider spend reserve/settle path healthy for low-risk slice",
    "verification_independence": "Builder cannot self-verify when independence is required",
}


@dataclass
class SafetyGate:
    key: str
    status: GateStatus = GateStatus.unknown
    evidence_ref: str | None = None
    notes: str = ""
    updated_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "status": self.status.value,
            "evidence_ref": self.evidence_ref,
            "notes": self.notes,
            "description": GATE_DESCRIPTIONS.get(self.key, ""),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class ActivationGateSet:
    """Single authoritative activation check — not scattered conditionals."""

    gates: dict[str, SafetyGate] = field(default_factory=dict)

    @classmethod
    def blank(cls) -> "ActivationGateSet":
        return cls(gates={k: SafetyGate(key=k, status=GateStatus.unknown) for k in REQUIRED_ACTIVATION_GATES})

    def record(
        self,
        key: str,
        *,
        status: GateStatus,
        evidence_ref: str | None = None,
        notes: str = "",
    ) -> SafetyGate:
        if key not in REQUIRED_ACTIVATION_GATES:
            raise ValueError(f"unknown activation gate: {key}")
        if status == GateStatus.verified and not evidence_ref:
            raise ValueError("VERIFIED requires evidence_ref (UNKNOWN != VERIFIED)")
        g = SafetyGate(
            key=key,
            status=status,
            evidence_ref=evidence_ref,
            notes=notes,
            updated_at=datetime.utcnow(),
        )
        self.gates[key] = g
        return g

    def evaluate(self) -> "ActivationDecision":
        missing_unknown: list[str] = []
        failed: list[str] = []
        verified: list[str] = []
        for key in REQUIRED_ACTIVATION_GATES:
            g = self.gates.get(key) or SafetyGate(key=key, status=GateStatus.unknown)
            if g.status == GateStatus.verified:
                verified.append(key)
            elif g.status == GateStatus.failed:
                failed.append(key)
            else:
                missing_unknown.append(key)
        allowed = not missing_unknown and not failed
        reason = (
            "all_gates_verified"
            if allowed
            else (
                "FAIL_CLOSED: "
                + (", ".join(f"{k}=failed" for k in failed) if failed else "")
                + ("; " if failed and missing_unknown else "")
                + (", ".join(f"{k}=unknown" for k in missing_unknown) if missing_unknown else "")
            )
        )
        return ActivationDecision(
            allowed=allowed,
            reason=reason,
            verified=tuple(verified),
            unknown=tuple(missing_unknown),
            failed=tuple(failed),
            snapshot={k: (self.gates.get(k) or SafetyGate(key=k)).as_dict() for k in REQUIRED_ACTIVATION_GATES},
        )


@dataclass(frozen=True)
class ActivationDecision:
    allowed: bool
    reason: str
    verified: tuple[str, ...]
    unknown: tuple[str, ...]
    failed: tuple[str, ...]
    snapshot: dict

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "verified": list(self.verified),
            "unknown": list(self.unknown),
            "failed": list(self.failed),
            "snapshot": self.snapshot,
            "invariant": "UNKNOWN_NE_VERIFIED",
        }


# Process-local default gate set — starts ALL UNKNOWN (fail closed).
_ACTIVE_GATES: ActivationGateSet = ActivationGateSet.blank()


def get_activation_gates() -> ActivationGateSet:
    return _ACTIVE_GATES


def reset_activation_gates_for_tests() -> None:
    global _ACTIVE_GATES
    _ACTIVE_GATES = ActivationGateSet.blank()


def record_gate_verification(
    key: str,
    *,
    status: GateStatus | str = GateStatus.verified,
    evidence_ref: str | None = None,
    notes: str = "",
) -> SafetyGate:
    st = GateStatus(status) if isinstance(status, str) else status
    return _ACTIVE_GATES.record(key, status=st, evidence_ref=evidence_ref, notes=notes)


def require_activation_allowed() -> ActivationDecision:
    decision = _ACTIVE_GATES.evaluate()
    if not decision.allowed:
        raise ProviderActivationBlocked(decision.reason)
    return decision


# --- Compatibility shims for older provider_worker API names ---

# Map legacy short names used in earlier harness to canonical keys.
_LEGACY_ALIAS: dict[str, str] = {
    "personal_intent_consequential_confirmation": "pr_218_consequential_confirmation",
    "self_model_evidence_gates": "pr_213_self_model_evidence",
    "current_truth_selection": "pr_224_current_truth_selection",
    "same_collapse_race": "pr_229_same_collapse_race",
    "workforce_authority_context_isolation": "workforce_context_isolation",
}


def mark_safety_gate(gate: str, *, satisfied: bool = True) -> None:
    """Legacy helper — satisfied=True still requires evidence_ref via record_gate_verification.

    Prefer record_gate_verification(...). This shim marks VERIFIED only with a synthetic
    test evidence ref when used from tests; production callers must use record_gate_verification.
    """
    key = _LEGACY_ALIAS.get(gate, gate)
    if satisfied:
        record_gate_verification(
            key,
            status=GateStatus.verified,
            evidence_ref=f"legacy_mark:{key}",
            notes="via mark_safety_gate shim",
        )
    else:
        record_gate_verification(key, status=GateStatus.unknown, evidence_ref=None, notes="cleared")


def reset_safety_gates_for_tests() -> None:
    reset_activation_gates_for_tests()


def activation_gate_status() -> dict[str, bool]:
    snap = _ACTIVE_GATES.evaluate().snapshot
    return {k: (v["status"] == GateStatus.verified.value) for k, v in snap.items()}


def activation_allowed() -> tuple[bool, list[str]]:
    d = _ACTIVE_GATES.evaluate()
    blocked = list(d.unknown) + list(d.failed)
    return d.allowed, blocked
