"""Guardian / Trust Kernel — core types (MainAI V2, Stage V2-B).

Standalone, isolated, NOT imported by any production runtime path. See
docs/mainai_v2/MAINAI_V2_GUARDIAN_TRUST_KERNEL.md for the design this implements.

Guardian is deliberately dumb: no model calls, no "reasoning" — mechanical, auditable,
table-driven decisions only. `GuardianDecision` is a distinct type from every *Request*
type on purpose: a requester (MainAI, an agent, Sentinel) can only ever construct a
*Request*. Only `app.guardian.service`'s own decision functions can construct a
`GuardianDecision` — the type system, not convention, is what prevents a requester from
"deciding for" Guardian.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Decision vocabulary: deliberately small and closed. ------------------------------


class GuardianAction(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REDUCE = "REDUCE"
    ISOLATE = "ISOLATE"
    LOCK = "LOCK"
    REQUIRE_OWNER = "REQUIRE_OWNER"
    ENTER_RECOVERY = "ENTER_RECOVERY"


class GuardianReason(str, Enum):
    """Closed vocabulary for WHY a decision was made — never a free-form LLM sentence."""

    WITHIN_POLICY = "WITHIN_POLICY"
    EXCEEDS_CEILING = "EXCEEDS_CEILING"
    REQUESTER_CANNOT_RAISE_OWN_CEILING = "REQUESTER_CANNOT_RAISE_OWN_CEILING"
    UNKNOWN_SECURITY_STATE = "UNKNOWN_SECURITY_STATE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    DEVICE_TRUST_REVOKED = "DEVICE_TRUST_REVOKED"
    SCOPE_OR_ANCESTOR_STOPPED = "SCOPE_OR_ANCESTOR_STOPPED"
    OWNER_IDENTITY_REQUIRED = "OWNER_IDENTITY_REQUIRED"
    OWNER_IDENTITY_VERIFIED = "OWNER_IDENTITY_VERIFIED"
    SELF_ISOLATION_REQUEST_ACCEPTED = "SELF_ISOLATION_REQUEST_ACCEPTED"
    SELF_ISOLATION_REQUEST_RATE_LIMITED = "SELF_ISOLATION_REQUEST_RATE_LIMITED"
    RECOVERY_TRIGGER_ACCEPTED = "RECOVERY_TRIGGER_ACCEPTED"
    RECOVERY_NOT_YET_COMPLETE = "RECOVERY_NOT_YET_COMPLETE"
    POLICY_VERSION_STALE = "POLICY_VERSION_STALE"
    POLICY_SIGNATURE_INVALID = "POLICY_SIGNATURE_INVALID"


class ContainmentScope(str, Enum):
    """Generalizes app.workforce.kill_switch's workforce_authority_epoch scope_key.

    Ordering matters: GLOBAL > OWNER > everything else. A stop at a broader scope
    transitively blocks every narrower scope for the same owner (see service.py's
    _ancestor_scopes()).
    """

    GLOBAL = "GLOBAL"
    OWNER = "OWNER"
    WORKFORCE = "WORKFORCE"
    NETWORK = "NETWORK"
    PROVIDER = "PROVIDER"
    VAULT = "VAULT"
    SENTINEL_RESPONSE = "SENTINEL_RESPONSE"


class DeviceTrustState(str, Enum):
    TRUSTED = "TRUSTED"
    UNKNOWN = "UNKNOWN"
    REVOKED = "REVOKED"


class IntegrityState(str, Enum):
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


# --- Requester-constructible request types (never a GuardianDecision). ----------------


@dataclass(frozen=True)
class AuthorityCeilingRequest:
    """MainAI or an agent PROPOSING a new ceiling. Never binding on its own — matches
    propose_execution_scope() vs authorize_execution_scope()."""

    scope: ContainmentScope
    owner_id: uuid.UUID
    requested_max_risk_level: str
    requested_by: str  # "mainai" | f"agent:{agent_id}" | "owner_explicit"
    reason: str


@dataclass(frozen=True)
class ContainmentRequest:
    scope: ContainmentScope
    owner_id: uuid.UUID | None  # None only valid for GLOBAL
    reason: str
    requested_by: str  # "guardian_self" | "mainai_self_isolation_request" | "owner_explicit" | "sentinel:<name>"


@dataclass(frozen=True)
class RecoveryTrigger:
    owner_id: uuid.UUID
    requested_by: str
    reason: str
    identity_assertion: "SovereignIdentityAssertion | None" = None


@dataclass(frozen=True)
class SovereignIdentityAssertion:
    """Stub for V2-G's real sovereign identity primitive. Guardian consumes this, never
    invents its own auth scheme (see design doc §3). This dataclass only carries what
    Guardian's own verification function needs — the actual cryptographic verification
    lives in guardian_verify_owner_for_recovery(), see policy.py for the real (HMAC-based,
    non-hardware-backed-yet) implementation."""

    claimed_owner_id: uuid.UUID
    challenge: str
    signature: str


# --- Guardian-only-constructible decision/state types. ---------------------------------


@dataclass(frozen=True)
class GuardianDecision:
    """Only ever constructed by app.guardian.service's decision functions. A requester
    (MainAI, an agent) cannot construct one of these for itself -- there is no public
    constructor path from a *Request type to this type other than going through Guardian's
    own policy evaluation."""

    action: GuardianAction
    reason: GuardianReason
    scope: ContainmentScope | None
    owner_id: uuid.UUID | None
    decided_at: datetime = field(default_factory=_utcnow)
    detail: str = ""


@dataclass(frozen=True)
class AuthorityCeiling:
    scope: ContainmentScope
    owner_id: uuid.UUID
    max_risk_level: str
    requires_owner_ack: bool
    set_by: str  # "owner_explicit" | "guardian_default" -- never "mainai"
    set_at: datetime = field(default_factory=_utcnow)


@dataclass
class ContainmentReceipt:
    """Immutable, append-only, hash-chained. See policy.py for the chain implementation."""

    receipt_id: uuid.UUID
    requested: object  # the *Request dataclass instance, kept for full audit trail
    decision: GuardianDecision
    policy_version: int
    prev_hash: str
    this_hash: str = ""  # computed and frozen at append time, see service.py
    created_at: datetime = field(default_factory=_utcnow)
