"""Sovereign Recovery state machine (MainAI V2, Stage V2-H1).

RECOVERY STARTED != RECOVERY COMPLETE: _VALID_RECOVERY_TRANSITIONS is a closed table --
RECOVERY_REQUESTED cannot reach RECOVERY_COMPLETE except by passing through every
intermediate state in order, one hop at a time. Every transition is recorded as a
hash-chained, append-only RecoveryReceipt, same discipline as app.guardian's
ContainmentReceipt chain and app.sentinel's EventReceipt chain.

RECOVERY != BACKDOOR: verify_recovery_identity() only ever returns True via a real
app.sovereign_identity OWNER_ROOT proof or a genuinely-satisfied, non-revoked threshold-share
count -- there is no path here that accepts a claimed identity as sufficient.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.sovereign_identity import InsufficientProofLevel, RootSensitiveAction, SessionIdentity, require_proof_level

from app.life_recovery.types import RecoveryMethodEnrollment, RecoveryReceipt, RecoveryState, RecoveryStateError

_GENESIS_HASH = "0" * 64

_VALID_RECOVERY_TRANSITIONS: dict[RecoveryState, frozenset[RecoveryState]] = {
    RecoveryState.NOT_CONFIGURED: frozenset({RecoveryState.READY}),
    RecoveryState.READY: frozenset({RecoveryState.RECOVERY_REQUESTED}),
    RecoveryState.RECOVERY_REQUESTED: frozenset({RecoveryState.IDENTITY_VERIFICATION, RecoveryState.REVOKED}),
    RecoveryState.IDENTITY_VERIFICATION: frozenset(
        {RecoveryState.CAPSULE_AVAILABLE, RecoveryState.RECOVERY_FAILED, RecoveryState.REVOKED}
    ),
    RecoveryState.CAPSULE_AVAILABLE: frozenset(
        {RecoveryState.KEY_UNLOCKED, RecoveryState.RECOVERY_FAILED, RecoveryState.REVOKED}
    ),
    RecoveryState.KEY_UNLOCKED: frozenset(
        {RecoveryState.CRITICAL_RESTORE, RecoveryState.RECOVERY_FAILED, RecoveryState.REVOKED}
    ),
    RecoveryState.CRITICAL_RESTORE: frozenset(
        {RecoveryState.BACKGROUND_RESTORE, RecoveryState.RECOVERY_FAILED, RecoveryState.REVOKED}
    ),
    RecoveryState.BACKGROUND_RESTORE: frozenset(
        {RecoveryState.RECOVERY_COMPLETE, RecoveryState.RECOVERY_FAILED, RecoveryState.REVOKED}
    ),
    RecoveryState.RECOVERY_COMPLETE: frozenset(),  # terminal
    RecoveryState.RECOVERY_FAILED: frozenset({RecoveryState.RECOVERY_REQUESTED}),  # retry only
    RecoveryState.REVOKED: frozenset(),  # terminal
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RecoveryStateMachine:
    """Mutable; owned/mutated only via this module's functions (mirrors GuardianState/
    SentinelState/IdentityState's discipline)."""

    recovery_id: uuid.UUID
    owner_id: uuid.UUID
    state: RecoveryState
    methods: dict[uuid.UUID, RecoveryMethodEnrollment] = field(default_factory=dict)
    receipts: list[RecoveryReceipt] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


def new_recovery_state_machine(*, owner_id: uuid.UUID) -> RecoveryStateMachine:
    return RecoveryStateMachine(recovery_id=uuid.uuid4(), owner_id=owner_id, state=RecoveryState.NOT_CONFIGURED)


def _receipt_hash(receipt: RecoveryReceipt) -> str:
    """Covers from_state, to_state, and reason -- not just the identifiers -- so none of
    those can be silently rewritten after the fact without breaking the chain (same "narrow
    hash" pitfall app.guardian.service._receipt_hash's docstring documents)."""
    payload = {
        "receipt_id": str(receipt.receipt_id),
        "from_state": receipt.from_state.value,
        "to_state": receipt.to_state.value,
        "reason": receipt.reason,
        "prev_hash": receipt.prev_hash,
        "created_at": receipt.created_at.isoformat(),
    }
    canonical = "|".join(f"{k}={v}" for k, v in sorted(payload.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_transition(machine: RecoveryStateMachine, *, to_state: RecoveryState, reason: str) -> RecoveryReceipt:
    prev_hash = machine.receipts[-1].this_hash if machine.receipts else _GENESIS_HASH
    receipt = RecoveryReceipt(
        receipt_id=uuid.uuid4(), from_state=machine.state, to_state=to_state, reason=reason, prev_hash=prev_hash
    )
    object.__setattr__(receipt, "this_hash", _receipt_hash(receipt))
    machine.receipts.append(receipt)
    machine.state = to_state
    machine.updated_at = _utcnow()
    return receipt


def advance_recovery(machine: RecoveryStateMachine, *, to_state: RecoveryState, reason: str) -> RecoveryReceipt:
    """RECOVERY STARTED != RECOVERY COMPLETE: raises RecoveryStateError for any transition
    not present in _VALID_RECOVERY_TRANSITIONS[machine.state] -- REQUESTED -> COMPLETE in one
    hop is structurally unreachable, not merely discouraged."""
    allowed = _VALID_RECOVERY_TRANSITIONS.get(machine.state, frozenset())
    if to_state not in allowed:
        raise RecoveryStateError(
            f"cannot transition recovery {machine.recovery_id} from {machine.state.value} to {to_state.value} -- "
            f"allowed next states are {sorted(s.value for s in allowed) or '(none, terminal state)'}"
        )
    return _record_transition(machine, to_state=to_state, reason=reason)


def verify_receipt_chain_intact(machine: RecoveryStateMachine) -> bool:
    prev = _GENESIS_HASH
    for receipt in machine.receipts:
        if receipt.prev_hash != prev:
            return False
        if _receipt_hash(receipt) != receipt.this_hash:
            return False
        prev = receipt.this_hash
    return True


def enroll_recovery_method(
    machine: RecoveryStateMachine, *, recovery_identity_id: uuid.UUID, kind_label: str, threshold: int = 1, required_shares: int = 1
) -> RecoveryMethodEnrollment:
    enrollment = RecoveryMethodEnrollment(
        recovery_identity_id=recovery_identity_id, kind_label=kind_label, threshold=threshold, required_shares=required_shares
    )
    machine.methods[recovery_identity_id] = enrollment
    return enrollment


def revoke_recovery_method(machine: RecoveryStateMachine, *, recovery_identity_id: uuid.UUID, reason: str) -> None:
    """Recovery factor revocation: once revoked, use_recovery_method() rejects it even if the
    underlying device/key material would otherwise still work."""
    from dataclasses import replace

    existing = machine.methods.get(recovery_identity_id)
    if existing is None:
        raise RecoveryStateError(f"no recovery method enrolled with id {recovery_identity_id}")
    machine.methods[recovery_identity_id] = replace(existing, revoked=True, revoked_reason=reason)


def use_recovery_method(machine: RecoveryStateMachine, *, recovery_identity_id: uuid.UUID, presented_shares: int) -> bool:
    """Returns True only if the method exists, is not revoked, and enough shares were
    presented to satisfy its threshold. Never returns True for a revoked method."""
    method = machine.methods.get(recovery_identity_id)
    if method is None:
        return False
    if method.revoked:
        return False
    return presented_shares >= method.threshold


def verify_recovery_identity(
    machine: RecoveryStateMachine,
    *,
    session: SessionIdentity | None = None,
    recovery_identity_id: uuid.UUID | None = None,
    presented_shares: int = 0,
) -> bool:
    """RECOVERY != BACKDOOR: real verification only. Path 1: a SessionIdentity that
    genuinely carries OWNER_ROOT proof (checked via the real require_proof_level(), never a
    local re-implementation). Path 2: a non-revoked enrolled recovery method with enough
    presented shares to meet its threshold. No other path returns True -- a caller merely
    claiming to be "LifeAI support" or the owner has no code path here that succeeds."""
    if session is not None:
        try:
            require_proof_level(session, RootSensitiveAction.AUTHORIZE_RECOVERY_METHOD)
            return True
        except InsufficientProofLevel:
            pass
    if recovery_identity_id is not None:
        return use_recovery_method(machine, recovery_identity_id=recovery_identity_id, presented_shares=presented_shares)
    return False
