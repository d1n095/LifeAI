"""Sovereign Recovery + Life Image + Hydration -- orchestration service (MainAI V2, Stages
V2-H1, V2-H2, V2-H4).

RecoveryEnvironment is the single container a caller interacts with -- mirrors
GuardianState/SentinelState/IdentityState's "one state object, mutated only through this
module's functions" discipline. Bundles a RecoveryStateMachine (V2-H1) and a
HydrationProgress (V2-H4) under one owner-scoped snapshot round-trip.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.life_recovery import hydration as _hydration
from app.life_recovery import recovery_state as _recovery_state
from app.life_recovery.recovery_state import RecoveryStateMachine
from app.life_recovery.types import (
    HydrationProgress,
    RecoveryReceipt,
    RecoveryState,
    RestoreDrillState,
)
from app.life_recovery.life_image import run_backup_verification


@dataclass
class RecoveryEnvironment:
    owner_id: uuid.UUID
    recovery: RecoveryStateMachine
    hydration: HydrationProgress


def new_recovery_environment(*, owner_id: uuid.UUID) -> RecoveryEnvironment:
    return RecoveryEnvironment(
        owner_id=owner_id,
        recovery=_recovery_state.new_recovery_state_machine(owner_id=owner_id),
        hydration=_hydration.new_hydration_progress(owner_id=owner_id),
    )


def determine_restore_drill_state(*args, **kwargs) -> RestoreDrillState:
    """BACKUP EXISTS != BACKUP RESTORES: the ONLY function that may produce
    RestoreDrillState.RESTORE_TESTED -- and only when run_backup_verification()'s result
    actually passed every check. A caller who only ever created/uploaded a backup and never
    called this gets no better than UNTESTED (the BackupRecord's own default)."""
    result = run_backup_verification(*args, **kwargs)
    if result.passed:
        return RestoreDrillState.RESTORE_TESTED
    if result.manifest_valid and result.components_available_ok:
        return RestoreDrillState.PARTIALLY_VERIFIED
    return RestoreDrillState.FAILED


# --- Serialization round-trip. ------------------------------------------------------------


def _receipt_to_dict(r: RecoveryReceipt) -> dict:
    return {
        "receipt_id": str(r.receipt_id),
        "from_state": r.from_state.value,
        "to_state": r.to_state.value,
        "reason": r.reason,
        "prev_hash": r.prev_hash,
        "this_hash": r.this_hash,
        "created_at": r.created_at.isoformat(),
    }


def _receipt_from_dict(d: dict) -> RecoveryReceipt:
    return RecoveryReceipt(
        receipt_id=uuid.UUID(d["receipt_id"]),
        from_state=RecoveryState(d["from_state"]),
        to_state=RecoveryState(d["to_state"]),
        reason=d["reason"],
        prev_hash=d["prev_hash"],
        this_hash=d["this_hash"],
        created_at=datetime.fromisoformat(d["created_at"]),
    )


def _method_to_dict(m) -> dict:
    return {
        "recovery_identity_id": str(m.recovery_identity_id),
        "kind_label": m.kind_label,
        "threshold": m.threshold,
        "required_shares": m.required_shares,
        "enrolled_at": m.enrolled_at.isoformat(),
        "revoked": m.revoked,
        "revoked_reason": m.revoked_reason,
    }


def _method_from_dict(d: dict):
    from app.life_recovery.types import RecoveryMethodEnrollment

    return RecoveryMethodEnrollment(
        recovery_identity_id=uuid.UUID(d["recovery_identity_id"]),
        kind_label=d["kind_label"],
        threshold=d["threshold"],
        required_shares=d["required_shares"],
        enrolled_at=datetime.fromisoformat(d["enrolled_at"]),
        revoked=d["revoked"],
        revoked_reason=d["revoked_reason"],
    )


def to_snapshot(env: RecoveryEnvironment) -> dict:
    """JSON-safe snapshot, explicit (not pickle) -- same rationale as
    app.guardian/app.sentinel/app.sovereign_identity's own to_snapshot()s. Contains no key
    material at all (RecoveryEnvironment never holds a DEK/KEK -- see LocalKeyStore in
    reset.py for the one place this whole package holds key bytes, which is deliberately
    NOT part of this snapshot)."""
    return {
        "owner_id": str(env.owner_id),
        "recovery": {
            "recovery_id": str(env.recovery.recovery_id),
            "owner_id": str(env.recovery.owner_id),
            "state": env.recovery.state.value,
            "methods": {str(k): _method_to_dict(v) for k, v in env.recovery.methods.items()},
            "receipts": [_receipt_to_dict(r) for r in env.recovery.receipts],
            "created_at": env.recovery.created_at.isoformat(),
            "updated_at": env.recovery.updated_at.isoformat(),
        },
        "hydration": _hydration.to_snapshot(env.hydration),
    }


def from_snapshot(snapshot: dict) -> RecoveryEnvironment:
    recovery = RecoveryStateMachine(
        recovery_id=uuid.UUID(snapshot["recovery"]["recovery_id"]),
        owner_id=uuid.UUID(snapshot["recovery"]["owner_id"]),
        state=RecoveryState(snapshot["recovery"]["state"]),
        methods={uuid.UUID(k): _method_from_dict(v) for k, v in snapshot["recovery"]["methods"].items()},
        receipts=[_receipt_from_dict(r) for r in snapshot["recovery"]["receipts"]],
        created_at=datetime.fromisoformat(snapshot["recovery"]["created_at"]),
        updated_at=datetime.fromisoformat(snapshot["recovery"]["updated_at"]),
    )
    hydration_progress = _hydration.from_snapshot(snapshot["hydration"])
    return RecoveryEnvironment(owner_id=uuid.UUID(snapshot["owner_id"]), recovery=recovery, hydration=hydration_progress)
