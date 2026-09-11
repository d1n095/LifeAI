"""Device Trust (MainAI V2, Stage V2-H3).

DEVICE WAS TRUSTED != DEVICE IS TRUSTED -- trust is a current, revocable state per device,
not a one-time onboarding fact. Revocation keys off device_id + a monotonically-increasing
`generation` counter (incremented on every fresh enrollment), never off the presence or
freshness of an enrollment record -- see reject_stale_enrollment() for why that distinction
is what actually stops an old-enrollment replay, and why re-presenting a device's old
(pre-revocation) key can never restore trust.

Functions here operate on a plain `dict[str, DeviceRecord]` rather than a full state object,
to avoid a circular import with service.py (which owns that dict inside IdentityState) --
same shape as app.sentinel.correlation operating on state's underlying collections directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.sovereign_identity.types import DeviceKeyGrant, DeviceRecord, DeviceTrustError, DeviceTrustLevel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enroll_device(
    devices: dict[str, DeviceRecord], *, device_id: str, owner_id: uuid.UUID, public_identity: str
) -> DeviceRecord:
    """A fresh enrollment, starting at PENDING (never TRUSTED on enrollment alone -- see
    approve_device()). If this device_id was previously revoked, the generation counter
    increments from whatever it last was -- it never resets to a value an old, pre-
    revocation enrollment record could match."""
    existing = devices.get(device_id)
    generation = (existing.generation + 1) if existing is not None else 1
    record = DeviceRecord(
        device_id=device_id,
        owner_id=owner_id,
        public_identity=public_identity,
        generation=generation,
        enrolled_at=_utcnow(),
        last_verified=_utcnow(),
        trust_state=DeviceTrustLevel.PENDING,
        key_grants=(),
        sync_scope=(),
    )
    devices[device_id] = record
    return record


def approve_device(devices: dict[str, DeviceRecord], *, device_id: str) -> DeviceRecord:
    """Requires a separate, explicit root-authority-verified approval step in a real
    integration (docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md §3 step 4) -- this function
    only performs the resulting state transition, it does not itself check proof level
    (that composition is for a future caller / cross-layer test, see
    root_authority.require_proof_level-style gating in service.py)."""
    record = devices[device_id]
    if record.trust_state == DeviceTrustLevel.REVOKED:
        raise DeviceTrustError(
            f"device {device_id} is REVOKED -- cannot be re-approved by reconnecting; it must re-enroll as a "
            "new device (see enroll_device(), which bumps the generation counter)"
        )
    record.trust_state = DeviceTrustLevel.TRUSTED
    record.last_verified = _utcnow()
    return record


def grant_device_key(devices: dict[str, DeviceRecord], *, device_id: str, grant: DeviceKeyGrant) -> DeviceRecord:
    """An untrusted (PENDING/RESTRICTED/SUSPECTED) or revoked device cannot receive a
    sensitive key grant -- only TRUSTED. This is checked fresh on every call against the
    device's CURRENT trust_state, so a device revoked after an earlier successful grant
    cannot receive any further ones."""
    record = devices[device_id]
    if record.trust_state != DeviceTrustLevel.TRUSTED:
        raise DeviceTrustError(
            f"device {device_id} is {record.trust_state.value}, not TRUSTED -- cannot receive a key grant"
        )
    record.key_grants = record.key_grants + (grant,)
    return record


def grant_sync_scope(devices: dict[str, DeviceRecord], *, device_id: str, scope: tuple[str, ...]) -> DeviceRecord:
    """Same TRUSTED-only gate as grant_device_key() -- a revoked device cannot sync,
    checked fresh against current trust_state every call."""
    record = devices[device_id]
    if record.trust_state != DeviceTrustLevel.TRUSTED:
        raise DeviceTrustError(f"device {device_id} is {record.trust_state.value}, not TRUSTED -- cannot sync")
    record.sync_scope = scope
    return record


def reject_stale_enrollment(devices: dict[str, DeviceRecord], *, device_id: str, presented_generation: int) -> None:
    """Raises DeviceTrustError for an old-enrollment replay: a device presenting an
    enrollment record issued BEFORE its most recent revocation, trying to look freshly-
    trusted. Rejected because this checks device_id + the CURRENT generation counter, not
    the presented record's own claimed trust_state or freshness -- a presented generation
    older than the record's current generation is rejected unconditionally."""
    record = devices.get(device_id)
    if record is None:
        raise DeviceTrustError(f"device {device_id} is not enrolled")
    if presented_generation < record.generation:
        raise DeviceTrustError(
            f"stale enrollment: presented generation {presented_generation} is older than current generation "
            f"{record.generation} -- this is a replay of a pre-revocation enrollment record"
        )


def revoke_device(devices: dict[str, DeviceRecord], *, device_id: str, reason: str) -> DeviceRecord:
    """The 4-step revocation flow (docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md §2), all
    applied atomically within this one function call -- no partial-revoke half-state is
    reachable from outside it:
      1. trust_state -> REVOKED.
      2. Freeze sync (sync_scope cleared).
      3. Invalidate active key grants (key_grants cleared -- this removes only THIS
         device's own copies/access records, never the underlying ciphertext, which is
         preserved via other trusted devices or the Recovery Capsule, per the design doc).
      4. Prevent future key wrapping: REVOKED trust_state already blocks
         grant_device_key()/grant_sync_scope() above; capability_leases are also cleared
         here.
    Revocation is NEVER destructive to already-stored ciphertext -- nothing in this
    function deletes or touches any encrypted data, only this device's own access records.
    """
    record = devices[device_id]
    record.trust_state = DeviceTrustLevel.REVOKED
    record.revoked_at = _utcnow()
    record.reason = reason
    record.sync_scope = ()
    record.key_grants = ()
    record.capability_leases = ()
    return record


def flag_lost_or_compromised(devices: dict[str, DeviceRecord], *, device_id: str, reason: str) -> DeviceRecord:
    """Owner-initiated lost/compromised flow: revoke -> stop new key wrapping (implied by
    REVOKED trust_state, enforced by grant_device_key/grant_sync_scope's gate) -> invalidate
    leases -> freeze sensitive sync -> preserve encrypted backups (never touched by this
    function) -> [a local-containment hook point for a future Sentinel/Guardian
    composition exists conceptually here, but is deliberately NOT implemented in this
    package -- see module docstring's independence discipline] -> recovery on a new trusted
    device remains possible via a fresh enroll_device() call for a new device_id. Does NOT
    attempt remote wipe or any "hack-back" capability -- explicitly out of scope per the
    founder's spec."""
    return revoke_device(devices, device_id=device_id, reason=f"lost_or_compromised: {reason}")
