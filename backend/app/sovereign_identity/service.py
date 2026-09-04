"""Sovereign Identity + Key Hierarchy + Device Trust -- orchestration service (MainAI V2,
Stages V2-G1, V2-G2, V2-H3).

IdentityState is the single container a caller interacts with -- mirrors GuardianState/
SentinelState's "one state object, mutated only through this module's functions, no public
method that removes history" discipline.

Note what IdentityState deliberately does NOT hold: any DEK or KEK plaintext bytes. Callers
generate those explicitly via keys.generate_key_material() and pass them around themselves;
this state only ever stores WrappedKey (already ciphertext, inside a DeviceKeyGrant) and
public key material (OwnerRootCapability.public_key). This is a structural property, not a
policy choice enforced by a check -- there is no field anywhere in IdentityState shaped to
hold a raw DEK/KEK, so to_snapshot() below cannot leak one even by omission-bug, only by
someone adding a new field that shouldn't exist (see
test_no_plaintext_key_material_in_repr_or_snapshot).
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.sovereign_identity.device_trust import (
    approve_device as _approve_device,
    enroll_device as _enroll_device,
    flag_lost_or_compromised as _flag_lost_or_compromised,
    grant_device_key as _grant_device_key,
    grant_sync_scope as _grant_sync_scope,
    reject_stale_enrollment as _reject_stale_enrollment,
    revoke_device as _revoke_device,
)
from app.sovereign_identity.root_authority import (
    RootAuthorityProof,
    RootAuthorityState,
    ChallengeRecord,
    evaluate_identity_assertion as _evaluate_identity_assertion,
    issue_challenge as _issue_challenge,
)
from app.sovereign_identity.types import (
    DeviceKeyGrant,
    DeviceRecord,
    DeviceTrustLevel,
    HardwareCapabilityStatus,
    InsufficientProofLevel,
    KeyPurpose,
    KeyVersion,
    OwnerRootCapability,
    ProofLevel,
    RootSensitiveAction,
    SessionIdentity,
    WrappedKey,
)

_ROOT_SENSITIVE_ACTIONS = frozenset(RootSensitiveAction)


@dataclass
class IdentityState:
    owner_id: uuid.UUID
    root_authority: RootAuthorityState = field(default_factory=RootAuthorityState)
    _devices: dict[str, DeviceRecord] = field(default_factory=dict)
    _owner_root_capability: OwnerRootCapability | None = None
    _key_versions: dict[tuple[str, str], KeyVersion] = field(default_factory=dict)

    def devices_snapshot(self) -> tuple[DeviceRecord, ...]:
        return tuple(self._devices.values())

    def device(self, device_id: str) -> DeviceRecord:
        return self._devices[device_id]


def new_identity_state(*, owner_id: uuid.UUID) -> IdentityState:
    return IdentityState(owner_id=owner_id)


# --- Root authority. --------------------------------------------------------------------


def register_owner_root_key(state: IdentityState, *, public_key: bytes) -> OwnerRootCapability:
    capability = OwnerRootCapability(owner_id=state.owner_id, public_key=public_key)
    state._owner_root_capability = capability
    state.root_authority.register_root_public_key(owner_id=state.owner_id, public_key=public_key)
    return capability


def issue_root_challenge(state: IdentityState) -> tuple[uuid.UUID, bytes]:
    return _issue_challenge(state.root_authority, owner_id=state.owner_id)


def evaluate_identity_assertion(
    state: IdentityState, *, claimed_level: ProofLevel, device_id: str | None, proof: RootAuthorityProof | None
) -> SessionIdentity:
    return _evaluate_identity_assertion(
        state.root_authority, owner_id=state.owner_id, claimed_level=claimed_level, device_id=device_id, proof=proof
    )


def require_proof_level(identity: SessionIdentity, action: RootSensitiveAction) -> None:
    """The root-sensitive-action gate (V2-G1). Every RootSensitiveAction requires
    OWNER_ROOT, never less -- raises InsufficientProofLevel (never silently returns a bare
    False a caller could accidentally ignore) for anything below it. `action` must be a
    real RootSensitiveAction member; this function makes no claim about actions outside
    that closed enum."""
    if action not in _ROOT_SENSITIVE_ACTIONS:
        raise ValueError(f"{action!r} is not a recognized RootSensitiveAction")
    if identity.proof_level != ProofLevel.OWNER_ROOT:
        raise InsufficientProofLevel(
            f"{action.value} requires OWNER_ROOT proof, but this identity has {identity.proof_level.value}"
        )


# --- Device trust (thin delegation so callers only ever import from `service`). ---------


def enroll_device(state: IdentityState, *, device_id: str, public_identity: str) -> DeviceRecord:
    return _enroll_device(state._devices, device_id=device_id, owner_id=state.owner_id, public_identity=public_identity)


def approve_device(state: IdentityState, *, device_id: str) -> DeviceRecord:
    return _approve_device(state._devices, device_id=device_id)


def grant_device_key(state: IdentityState, *, device_id: str, grant: DeviceKeyGrant) -> DeviceRecord:
    return _grant_device_key(state._devices, device_id=device_id, grant=grant)


def grant_sync_scope(state: IdentityState, *, device_id: str, scope: tuple[str, ...]) -> DeviceRecord:
    return _grant_sync_scope(state._devices, device_id=device_id, scope=scope)


def reject_stale_enrollment(state: IdentityState, *, device_id: str, presented_generation: int) -> None:
    _reject_stale_enrollment(state._devices, device_id=device_id, presented_generation=presented_generation)


def revoke_device(state: IdentityState, *, device_id: str, reason: str) -> DeviceRecord:
    return _revoke_device(state._devices, device_id=device_id, reason=reason)


def flag_lost_or_compromised(state: IdentityState, *, device_id: str, reason: str) -> DeviceRecord:
    return _flag_lost_or_compromised(state._devices, device_id=device_id, reason=reason)


# --- Key version bookkeeping. -------------------------------------------------------------


def next_key_version(state: IdentityState, *, purpose: KeyPurpose) -> KeyVersion:
    """Rotation metadata: each call for a given purpose returns a new KeyVersion,
    referencing the prior version it rotated from (None on first use for that purpose)."""
    key = (purpose.value, str(state.owner_id))
    prior = state._key_versions.get(key)
    version = KeyVersion(
        purpose=purpose,
        owner_id=state.owner_id,
        version=(prior.version + 1) if prior else 1,
        rotated_from_version=prior.version if prior else None,
    )
    state._key_versions[key] = version
    return version


# --- Serialization round-trip. ------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def _wrapped_key_to_dict(wk: WrappedKey) -> dict:
    return {
        "purpose": wk.purpose.value,
        "owner_id": str(wk.owner_id),
        "key_version": wk.key_version,
        "wrapped": _b64(wk.wrapped),
        "nonce": _b64(wk.nonce),
        "algorithm": wk.algorithm,
        "wrapped_by_kek_version": wk.wrapped_by_kek_version,
    }


def _wrapped_key_from_dict(d: dict) -> WrappedKey:
    return WrappedKey(
        purpose=KeyPurpose(d["purpose"]),
        owner_id=uuid.UUID(d["owner_id"]),
        key_version=d["key_version"],
        wrapped=_unb64(d["wrapped"]),
        nonce=_unb64(d["nonce"]),
        algorithm=d["algorithm"],
        wrapped_by_kek_version=d["wrapped_by_kek_version"],
    )


def _device_key_grant_to_dict(g: DeviceKeyGrant) -> dict:
    return {
        "device_id": g.device_id,
        "owner_id": str(g.owner_id),
        "purpose": g.purpose.value,
        "key_version": g.key_version,
        "wrapped_key": _wrapped_key_to_dict(g.wrapped_key),
        "granted_at": g.granted_at.isoformat(),
    }


def _device_key_grant_from_dict(d: dict) -> DeviceKeyGrant:
    return DeviceKeyGrant(
        device_id=d["device_id"],
        owner_id=uuid.UUID(d["owner_id"]),
        purpose=KeyPurpose(d["purpose"]),
        key_version=d["key_version"],
        wrapped_key=_wrapped_key_from_dict(d["wrapped_key"]),
        granted_at=datetime.fromisoformat(d["granted_at"]),
    )


def _device_record_to_dict(r: DeviceRecord) -> dict:
    return {
        "device_id": r.device_id,
        "owner_id": str(r.owner_id),
        "public_identity": r.public_identity,
        "generation": r.generation,
        "enrolled_at": r.enrolled_at.isoformat(),
        "last_verified": r.last_verified.isoformat(),
        "trust_state": r.trust_state.value,
        "key_grants": [_device_key_grant_to_dict(g) for g in r.key_grants],
        "sync_scope": list(r.sync_scope),
        "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
        "reason": r.reason,
        "attestation_status": r.attestation_status.value,
        "capability_leases": list(r.capability_leases),
    }


def _device_record_from_dict(d: dict) -> DeviceRecord:
    return DeviceRecord(
        device_id=d["device_id"],
        owner_id=uuid.UUID(d["owner_id"]),
        public_identity=d["public_identity"],
        generation=d["generation"],
        enrolled_at=datetime.fromisoformat(d["enrolled_at"]),
        last_verified=datetime.fromisoformat(d["last_verified"]),
        trust_state=DeviceTrustLevel(d["trust_state"]),
        key_grants=tuple(_device_key_grant_from_dict(g) for g in d["key_grants"]),
        sync_scope=tuple(d["sync_scope"]),
        revoked_at=datetime.fromisoformat(d["revoked_at"]) if d["revoked_at"] else None,
        reason=d["reason"],
        attestation_status=HardwareCapabilityStatus(d["attestation_status"]),
        capability_leases=tuple(d["capability_leases"]),
    )


def to_snapshot(state: IdentityState) -> dict:
    """JSON-safe snapshot (bytes fields base64-encoded) -- explicit, not pickle, same
    rationale as app.guardian.service.to_snapshot(). Contains WrappedKey ciphertext
    (base64) and public key bytes (base64) -- never a plaintext DEK/KEK, because
    IdentityState never holds one in the first place (see module docstring)."""
    return {
        "owner_id": str(state.owner_id),
        "owner_root_capability": (
            {
                "owner_id": str(state._owner_root_capability.owner_id),
                "public_key": _b64(state._owner_root_capability.public_key),
                "enrolled_at": state._owner_root_capability.enrolled_at.isoformat(),
                "key_version": state._owner_root_capability.key_version,
            }
            if state._owner_root_capability is not None
            else None
        ),
        "root_public_keys": {k: _b64(v) for k, v in state.root_authority._root_public_keys.items()},
        "challenges": {
            k: {
                "owner_id": str(c.owner_id),
                "nonce": _b64(c.nonce),
                "issued_at": c.issued_at.isoformat(),
                "expires_at": c.expires_at.isoformat(),
                "used": c.used,
            }
            for k, c in state.root_authority._challenges.items()
        },
        "devices": {k: _device_record_to_dict(v) for k, v in state._devices.items()},
        "key_versions": {
            f"{k[0]}|{k[1]}": {
                "purpose": v.purpose.value,
                "owner_id": str(v.owner_id),
                "version": v.version,
                "created_at": v.created_at.isoformat(),
                "rotated_from_version": v.rotated_from_version,
            }
            for k, v in state._key_versions.items()
        },
    }


def from_snapshot(snapshot: dict) -> IdentityState:
    """Reconstructs an IdentityState from to_snapshot()'s output into a NEW object --
    round-trip is exercised by a real test that mutates state, serializes, deserializes,
    and asserts equality field-by-field, mirroring Guardian/Sentinel's own pattern."""
    state = IdentityState(owner_id=uuid.UUID(snapshot["owner_id"]))
    if snapshot["owner_root_capability"] is not None:
        c = snapshot["owner_root_capability"]
        state._owner_root_capability = OwnerRootCapability(
            owner_id=uuid.UUID(c["owner_id"]),
            public_key=_unb64(c["public_key"]),
            enrolled_at=datetime.fromisoformat(c["enrolled_at"]),
            key_version=c["key_version"],
        )
    state.root_authority._root_public_keys = {k: _unb64(v) for k, v in snapshot["root_public_keys"].items()}
    state.root_authority._challenges = {
        k: ChallengeRecord(
            owner_id=uuid.UUID(c["owner_id"]),
            nonce=_unb64(c["nonce"]),
            issued_at=datetime.fromisoformat(c["issued_at"]),
            expires_at=datetime.fromisoformat(c["expires_at"]),
            used=c["used"],
        )
        for k, c in snapshot["challenges"].items()
    }
    state._devices = {k: _device_record_from_dict(v) for k, v in snapshot["devices"].items()}
    for k, v in snapshot["key_versions"].items():
        purpose_val, owner_val = k.split("|", 1)
        state._key_versions[(purpose_val, owner_val)] = KeyVersion(
            purpose=KeyPurpose(v["purpose"]),
            owner_id=uuid.UUID(v["owner_id"]),
            version=v["version"],
            created_at=datetime.fromisoformat(v["created_at"]),
            rotated_from_version=v["rotated_from_version"],
        )
    return state
