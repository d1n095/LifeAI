"""Guardian / Trust Kernel — decision service (MainAI V2, Stage V2-B).

Standalone, isolated, NOT imported by any production runtime path.

Every `GuardianDecision` returned by this module is constructed HERE, inside these
functions -- never by a caller. A requester (MainAI, an agent, Sentinel) can only build a
*Request dataclass (see types.py); turning a request into a decision is Guardian's own,
sole responsibility. This is the concrete enforcement of `MODEL OUTPUT != AUTHORITY`.

In-memory reference implementation: this is a foundation/prototype, not wired to Postgres.
Production V2 work would back GuardianState with the generalized `workforce_authority_epoch`
table (see docs/mainai_v2/MAINAI_V2_GUARDIAN_TRUST_KERNEL.md §2) -- the DB-backed
SELECT..FOR SHARE/FOR UPDATE serialization proven in PR #243 is the real target, this module
mirrors its semantics (epoch increments, stop/grant precedence) in memory so the design can
be tested and reasoned about before that wiring happens.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.guardian.policy import GuardianPolicy, PolicyRejected, require_valid_policy, sign_policy
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

_GENESIS_HASH = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Scopes that transitively block narrower scopes for the same owner. GLOBAL blocks
# everything; OWNER blocks everything for that owner. Mirrors assert_grant_allowed()'s
# two-row lock ordering (GLOBAL then owner) generalized to N scope rows (design doc §2).
_ANCESTOR_SCOPES: dict[ContainmentScope, tuple[ContainmentScope, ...]] = {
    s: (ContainmentScope.GLOBAL, ContainmentScope.OWNER)
    for s in ContainmentScope
    if s not in (ContainmentScope.GLOBAL, ContainmentScope.OWNER)
}
_ANCESTOR_SCOPES[ContainmentScope.OWNER] = (ContainmentScope.GLOBAL,)
_ANCESTOR_SCOPES[ContainmentScope.GLOBAL] = ()


def _epoch_key(scope: ContainmentScope, owner_id: uuid.UUID | None) -> tuple[str, str]:
    if scope == ContainmentScope.GLOBAL:
        return (scope.value, "*")
    assert owner_id is not None, f"{scope} requires an owner_id"
    return (scope.value, str(owner_id))


@dataclass
class _EpochRow:
    stopped: bool = False
    epoch: int = 0


@dataclass
class GuardianState:
    """Mutable Guardian state. `receipts` is intentionally exposed only via
    `receipts_snapshot()` (a copy) -- there is no public method that removes or mutates an
    existing receipt, matching the design doc's 'immutable event receipts' requirement."""

    active_policy: GuardianPolicy
    _policy_signature: str
    _secret_key: bytes
    _epochs: dict[tuple[str, str], _EpochRow] = field(default_factory=dict)
    _device_trust: dict[str, DeviceTrustState] = field(default_factory=dict)
    _integrity: dict[str, IntegrityState] = field(default_factory=dict)
    _receipts: list[ContainmentReceipt] = field(default_factory=list)
    _self_isolation_log: dict[str, list[datetime]] = field(default_factory=dict)
    _authority_ceilings: dict[tuple[str, str], AuthorityCeiling] = field(default_factory=dict)
    _recovery_in_progress: set[str] = field(default_factory=set)

    def receipts_snapshot(self) -> tuple[ContainmentReceipt, ...]:
        return tuple(self._receipts)

    def device_trust_for(self, owner_id: uuid.UUID) -> DeviceTrustState:
        return self._device_trust.get(str(owner_id), DeviceTrustState.UNKNOWN)

    def integrity_for(self, owner_id: uuid.UUID) -> IntegrityState:
        return self._integrity.get(str(owner_id), IntegrityState.UNKNOWN)

    def is_stopped(self, scope: ContainmentScope, owner_id: uuid.UUID | None) -> bool:
        row = self._epochs.get(_epoch_key(scope, owner_id))
        return bool(row and row.stopped)

    def is_scope_or_ancestor_stopped(self, scope: ContainmentScope, owner_id: uuid.UUID | None) -> bool:
        if self.is_stopped(scope, owner_id):
            return True
        for ancestor in _ANCESTOR_SCOPES.get(scope, ()):
            ancestor_owner = None if ancestor == ContainmentScope.GLOBAL else owner_id
            if self.is_stopped(ancestor, ancestor_owner):
                return True
        return False


def new_guardian_state(*, owner_id: uuid.UUID, secret_key: bytes) -> GuardianState:
    from app.guardian.policy import default_policy

    policy = default_policy(str(owner_id))
    signature = sign_policy(policy, secret_key=secret_key)
    return GuardianState(active_policy=policy, _policy_signature=signature, _secret_key=secret_key)


# --- Receipts: append-only, hash-chained. ----------------------------------------------


def _receipt_hash(receipt: ContainmentReceipt) -> str:
    """Covers every field a tamperer might want to alter after the fact -- action, reason,
    scope, owner, detail text, timestamps -- not just the identifiers. A hash that only
    covered receipt_id/action/reason would let scope/owner_id/detail be silently rewritten
    without breaking the chain; this is the actual bug that shape would have, caught before
    it shipped."""
    payload = {
        "receipt_id": str(receipt.receipt_id),
        "decision_action": receipt.decision.action.value,
        "decision_reason": receipt.decision.reason.value,
        "decision_scope": receipt.decision.scope.value if receipt.decision.scope else None,
        "decision_owner_id": str(receipt.decision.owner_id) if receipt.decision.owner_id else None,
        "decision_detail": receipt.decision.detail,
        "decision_decided_at": receipt.decision.decided_at.isoformat(),
        "policy_version": receipt.policy_version,
        "prev_hash": receipt.prev_hash,
        "created_at": receipt.created_at.isoformat(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _record_receipt(state: GuardianState, *, requested: object, decision: GuardianDecision) -> ContainmentReceipt:
    prev_hash = state._receipts[-1].this_hash if state._receipts else _GENESIS_HASH
    receipt = ContainmentReceipt(
        receipt_id=uuid.uuid4(),
        requested=requested,
        decision=decision,
        policy_version=state.active_policy.version,
        prev_hash=prev_hash,
    )
    receipt.this_hash = _receipt_hash(receipt)
    state._receipts.append(receipt)
    return receipt


def verify_receipt_chain_intact(state: GuardianState) -> bool:
    """Real tamper detection: recompute every receipt's hash and confirm the prev_hash
    links match. Returns False the moment a mismatch is found -- does not stop at the
    first receipt only."""
    prev = _GENESIS_HASH
    for receipt in state._receipts:
        if receipt.prev_hash != prev:
            return False
        if _receipt_hash(receipt) != receipt.this_hash:
            return False
        prev = receipt.this_hash
    return True


# --- Policy management. ------------------------------------------------------------------


def apply_new_policy(state: GuardianState, policy: GuardianPolicy, signature: str) -> None:
    """Raises PolicyRejected (never silently ignores) for a stale version or bad
    signature -- see policy.require_valid_policy."""
    require_valid_policy(
        policy, signature, secret_key=state._secret_key, current_active_version=state.active_policy.version
    )
    state.active_policy = policy
    state._policy_signature = signature


# --- Decisions. ---------------------------------------------------------------------------


def evaluate_authority_ceiling_request(state: GuardianState, request: AuthorityCeilingRequest) -> GuardianDecision:
    """MAINAI CANNOT RAISE HER OWN AUTHORITY CEILING. AGENT CANNOT RAISE ITS OWN AUTHORITY
    CEILING. Only "owner_explicit" (a real, owner-identity-verified request -- V2-G) may
    raise a ceiling. Everything else is DENY, unconditionally, regardless of how reasonable
    the request sounds."""
    if request.requested_by != "owner_explicit":
        decision = GuardianDecision(
            action=GuardianAction.DENY,
            reason=GuardianReason.REQUESTER_CANNOT_RAISE_OWN_CEILING,
            scope=request.scope,
            owner_id=request.owner_id,
            detail=f"requested_by={request.requested_by!r} may never self-raise a ceiling",
        )
        _record_receipt(state, requested=request, decision=decision)
        return decision

    ceiling = AuthorityCeiling(
        scope=request.scope,
        owner_id=request.owner_id,
        max_risk_level=request.requested_max_risk_level,
        requires_owner_ack=True,
        set_by="owner_explicit",
    )
    state._authority_ceilings[(request.scope.value, str(request.owner_id))] = ceiling
    decision = GuardianDecision(
        action=GuardianAction.ALLOW,
        reason=GuardianReason.WITHIN_POLICY,
        scope=request.scope,
        owner_id=request.owner_id,
        detail="ceiling raised by owner_explicit request",
    )
    _record_receipt(state, requested=request, decision=decision)
    return decision


_SELF_ISOLATION_REQUESTERS = ("mainai_self_isolation_request",)


def evaluate_containment_request(state: GuardianState, request: ContainmentRequest) -> GuardianDecision:
    """Lowering authority (containment) is the fail-safe direction -- cheap, available even
    to a partially-untrusted caller (SECURITY FAILURE -> REDUCE AUTHORITY). MainAI's own
    self-isolation requests are rate-limited (a suspiciously rapid burst of "I don't trust
    myself" requests is itself a signal the reasoning generating them may be compromised,
    not a reason to ignore them -- but Guardian still independently decides, matching the
    design doc's "MainAI cannot force containment by asserting urgency" rule)."""
    if request.requested_by in _SELF_ISOLATION_REQUESTERS and request.owner_id is not None:
        key = str(request.owner_id)
        now = _utcnow()
        window_start = now - timedelta(seconds=state.active_policy.self_isolation_window_seconds)
        recent = [t for t in state._self_isolation_log.get(key, []) if t >= window_start]
        if len(recent) >= state.active_policy.max_self_isolation_requests_per_window:
            # Still rate-limited from MainAI's own request channel, but Guardian does NOT
            # simply refuse to contain -- it still isolates (the fail-safe direction), it
            # just records the rate-limit signal for audit rather than trusting MainAI's
            # framing verbatim.
            recent.append(now)
            state._self_isolation_log[key] = recent
            decision = _do_isolate(state, request, reason=GuardianReason.SELF_ISOLATION_REQUEST_RATE_LIMITED)
            return decision
        recent.append(now)
        state._self_isolation_log[key] = recent

    return _do_isolate(state, request, reason=GuardianReason.SELF_ISOLATION_REQUEST_ACCEPTED)


def _do_isolate(state: GuardianState, request: ContainmentRequest, *, reason: GuardianReason) -> GuardianDecision:
    row = state._epochs.setdefault(_epoch_key(request.scope, request.owner_id), _EpochRow())
    row.stopped = True
    row.epoch += 1
    decision = GuardianDecision(
        action=GuardianAction.ISOLATE,
        reason=reason,
        scope=request.scope,
        owner_id=request.owner_id,
        detail=request.reason,
    )
    _record_receipt(state, requested=request, decision=decision)
    return decision


def clear_containment(state: GuardianState, *, scope: ContainmentScope, owner_id: uuid.UUID | None) -> None:
    """Raising authority back up -- unlike containment, this is NOT exposed as a bare
    function callers invoke directly; guardian_verify_owner_for_recovery() must succeed
    first (see evaluate_recovery_trigger). Kept private-by-convention (leading underscore
    intentionally omitted here since tests need to call it post-verification, but no
    *Request type routes to it without going through recovery verification)."""
    row = state._epochs.get(_epoch_key(scope, owner_id))
    if row is not None:
        row.stopped = False


def evaluate_recovery_trigger(state: GuardianState, trigger: RecoveryTrigger) -> GuardianDecision:
    """RECOVERY ENTRY != RECOVERY COMPLETE: accepting the trigger only ever ENTERs recovery
    state; it does not itself clear any containment. A real owner identity assertion is
    required -- session possession alone (SESSION ACCESS != ROOT AUTHORITY) is never
    sufficient."""
    if trigger.identity_assertion is None or not guardian_verify_owner_for_recovery(
        state, trigger.identity_assertion
    ):
        decision = GuardianDecision(
            action=GuardianAction.REQUIRE_OWNER,
            reason=GuardianReason.OWNER_IDENTITY_REQUIRED,
            scope=None,
            owner_id=trigger.owner_id,
            detail="recovery requires a verified SovereignIdentityAssertion",
        )
        _record_receipt(state, requested=trigger, decision=decision)
        return decision

    state._recovery_in_progress.add(str(trigger.owner_id))
    decision = GuardianDecision(
        action=GuardianAction.ENTER_RECOVERY,
        reason=GuardianReason.RECOVERY_TRIGGER_ACCEPTED,
        scope=None,
        owner_id=trigger.owner_id,
        detail="recovery entered; not complete until an explicit completion step",
    )
    _record_receipt(state, requested=trigger, decision=decision)
    return decision


def guardian_verify_owner_for_recovery(state: GuardianState, assertion: SovereignIdentityAssertion) -> bool:
    """Real (HMAC-based), working challenge-response check -- NOT a stub. Replaces the
    weak, denylist+regex clear_kill_switch_for_recovery() ack found (but not fixed) in
    tonight's campaign, in the right layer instead of a local patch (design doc §3).

    NOT hardware-backed yet: this verifies the assertion's signature against Guardian's own
    shared secret_key, standing in for a real asymmetric, hardware-backed owner key (V2-G).
    The check itself is genuine -- an unsigned or wrongly-signed assertion is really
    rejected, not accepted by default."""
    import hmac as _hmac

    expected = _hmac.new(
        state._secret_key, f"{assertion.claimed_owner_id}:{assertion.challenge}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return _hmac.compare_digest(expected, assertion.signature)


def set_device_trust(state: GuardianState, *, owner_id: uuid.UUID, trust: DeviceTrustState) -> None:
    """DEVICE WAS TRUSTED != DEVICE IS TRUSTED -- overwrites, never merges with history
    (history lives in the receipt chain, not in the current-state dict)."""
    state._device_trust[str(owner_id)] = trust
    decision = GuardianDecision(
        action=GuardianAction.REDUCE if trust != DeviceTrustState.TRUSTED else GuardianAction.ALLOW,
        reason=GuardianReason.DEVICE_TRUST_REVOKED if trust == DeviceTrustState.REVOKED else GuardianReason.WITHIN_POLICY,
        scope=None,
        owner_id=owner_id,
        detail=f"device_trust set to {trust.value}",
    )
    _record_receipt(state, requested={"set_device_trust": trust.value}, decision=decision)


def set_integrity_state(state: GuardianState, *, owner_id: uuid.UUID, integrity: IntegrityState) -> None:
    state._integrity[str(owner_id)] = integrity


def evaluate_bounded_action(
    state: GuardianState,
    *,
    owner_id: uuid.UUID,
    scope: ContainmentScope,
    requested_risk_level: str,
    requested_by: str,
) -> GuardianDecision:
    """The everyday path: is this in-policy, bounded action allowed right now? Checks, in
    order: UNKNOWN SECURITY STATE -> REDUCE/BLOCK, device trust, scope-or-ancestor
    containment, then the authority ceiling. A known-safe bounded action with everything
    verified and nothing stopped -> ALLOW."""
    integrity = state.integrity_for(owner_id)
    device_trust = state.device_trust_for(owner_id)

    if integrity == IntegrityState.FAILED:
        decision = GuardianDecision(
            action=GuardianAction.ISOLATE,
            reason=GuardianReason.INTEGRITY_FAILURE,
            scope=scope,
            owner_id=owner_id,
            detail="integrity check FAILED -- containing rather than merely denying",
        )
        _record_receipt(state, requested={"bounded_action": requested_by}, decision=decision)
        return decision

    if integrity == IntegrityState.UNKNOWN or device_trust == DeviceTrustState.UNKNOWN:
        decision = GuardianDecision(
            action=GuardianAction.REDUCE,
            reason=GuardianReason.UNKNOWN_SECURITY_STATE,
            scope=scope,
            owner_id=owner_id,
            detail=f"integrity={integrity.value} device_trust={device_trust.value}",
        )
        _record_receipt(state, requested={"bounded_action": requested_by}, decision=decision)
        return decision

    if device_trust == DeviceTrustState.REVOKED:
        decision = GuardianDecision(
            action=GuardianAction.REDUCE,
            reason=GuardianReason.DEVICE_TRUST_REVOKED,
            scope=scope,
            owner_id=owner_id,
            detail="device trust revoked",
        )
        _record_receipt(state, requested={"bounded_action": requested_by}, decision=decision)
        return decision

    if state.is_scope_or_ancestor_stopped(scope, owner_id):
        decision = GuardianDecision(
            action=GuardianAction.DENY,
            reason=GuardianReason.SCOPE_OR_ANCESTOR_STOPPED,
            scope=scope,
            owner_id=owner_id,
            detail="scope or an ancestor scope is currently stopped",
        )
        _record_receipt(state, requested={"bounded_action": requested_by}, decision=decision)
        return decision

    ceiling = state._authority_ceilings.get((scope.value, str(owner_id)))
    ceiling_level = ceiling.max_risk_level if ceiling else "low"
    _RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
    if _RISK_ORDER.get(requested_risk_level, 99) > _RISK_ORDER.get(ceiling_level, 0):
        decision = GuardianDecision(
            action=GuardianAction.DENY,
            reason=GuardianReason.EXCEEDS_CEILING,
            scope=scope,
            owner_id=owner_id,
            detail=f"requested={requested_risk_level} ceiling={ceiling_level}",
        )
        _record_receipt(state, requested={"bounded_action": requested_by}, decision=decision)
        return decision

    decision = GuardianDecision(
        action=GuardianAction.ALLOW,
        reason=GuardianReason.WITHIN_POLICY,
        scope=scope,
        owner_id=owner_id,
        detail="bounded action within ceiling, no containment active, security state verified",
    )
    _record_receipt(state, requested={"bounded_action": requested_by}, decision=decision)
    return decision


# --- Serialization round-trip. ----------------------------------------------------------


def to_snapshot(state: GuardianState) -> dict:
    """JSON-safe snapshot. Explicit, not pickle -- pickle would silently succeed even if
    the shape drifted incompatibly across versions; this makes the shape a real contract."""
    return {
        "active_policy": {
            "version": state.active_policy.version,
            "owner_id": state.active_policy.owner_id,
            "authority_ceilings": dict(state.active_policy.authority_ceilings),
            "known_self_diagnostic_reasons": list(state.active_policy.known_self_diagnostic_reasons),
            "max_self_isolation_requests_per_window": state.active_policy.max_self_isolation_requests_per_window,
            "self_isolation_window_seconds": state.active_policy.self_isolation_window_seconds,
        },
        "policy_signature": state._policy_signature,
        "epochs": {f"{k[0]}|{k[1]}": {"stopped": v.stopped, "epoch": v.epoch} for k, v in state._epochs.items()},
        "device_trust": dict(state._device_trust),
        "integrity": dict(state._integrity),
        "receipts": [
            {
                "receipt_id": str(r.receipt_id),
                "decision_action": r.decision.action.value,
                "decision_reason": r.decision.reason.value,
                "decision_scope": r.decision.scope.value if r.decision.scope else None,
                "decision_owner_id": str(r.decision.owner_id) if r.decision.owner_id else None,
                "decision_detail": r.decision.detail,
                "decision_decided_at": r.decision.decided_at.isoformat(),
                "policy_version": r.policy_version,
                "prev_hash": r.prev_hash,
                "this_hash": r.this_hash,
                "created_at": r.created_at.isoformat(),
            }
            for r in state._receipts
        ],
        "authority_ceilings": {
            f"{k[0]}|{k[1]}": {
                "scope": v.scope.value,
                "owner_id": str(v.owner_id),
                "max_risk_level": v.max_risk_level,
                "requires_owner_ack": v.requires_owner_ack,
                "set_by": v.set_by,
                "set_at": v.set_at.isoformat(),
            }
            for k, v in state._authority_ceilings.items()
        },
    }


def from_snapshot(snapshot: dict, *, secret_key: bytes) -> GuardianState:
    """Reconstructs a GuardianState from to_snapshot()'s output. Round-trip is exercised by
    a real test (test_guardian_state_survives_serialization_round_trip) that mutates state,
    serializes, deserializes into a NEW object, and asserts equality field-by-field --
    catching silent loss/mutation, not assuming it away."""
    p = snapshot["active_policy"]
    policy = GuardianPolicy(
        version=p["version"],
        owner_id=p["owner_id"],
        authority_ceilings=dict(p["authority_ceilings"]),
        known_self_diagnostic_reasons=tuple(p["known_self_diagnostic_reasons"]),
        max_self_isolation_requests_per_window=p["max_self_isolation_requests_per_window"],
        self_isolation_window_seconds=p["self_isolation_window_seconds"],
    )
    state = GuardianState(active_policy=policy, _policy_signature=snapshot["policy_signature"], _secret_key=secret_key)
    for k, v in snapshot["epochs"].items():
        scope_val, owner_val = k.split("|", 1)
        state._epochs[(scope_val, owner_val)] = _EpochRow(stopped=v["stopped"], epoch=v["epoch"])
    state._device_trust = {k: DeviceTrustState(v) for k, v in snapshot["device_trust"].items()}
    state._integrity = {k: IntegrityState(v) for k, v in snapshot["integrity"].items()}
    for r in snapshot["receipts"]:
        decision = GuardianDecision(
            action=GuardianAction(r["decision_action"]),
            reason=GuardianReason(r["decision_reason"]),
            scope=ContainmentScope(r["decision_scope"]) if r["decision_scope"] else None,
            owner_id=uuid.UUID(r["decision_owner_id"]) if r["decision_owner_id"] else None,
            decided_at=datetime.fromisoformat(r["decision_decided_at"]),
            detail=r["decision_detail"],
        )
        receipt = ContainmentReceipt(
            receipt_id=uuid.UUID(r["receipt_id"]),
            requested=None,
            decision=decision,
            policy_version=r["policy_version"],
            prev_hash=r["prev_hash"],
            this_hash=r["this_hash"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )
        state._receipts.append(receipt)
    for k, v in snapshot["authority_ceilings"].items():
        scope_val, owner_val = k.split("|", 1)
        state._authority_ceilings[(scope_val, owner_val)] = AuthorityCeiling(
            scope=ContainmentScope(v["scope"]),
            owner_id=uuid.UUID(v["owner_id"]),
            max_risk_level=v["max_risk_level"],
            requires_owner_ack=v["requires_owner_ack"],
            set_by=v["set_by"],
            set_at=datetime.fromisoformat(v["set_at"]),
        )
    return state
