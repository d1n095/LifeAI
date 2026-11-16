"""Recovery Capsule build/verify (MainAI V2, Stage V2-H1).

Fleshes out app.sovereign_identity.types.RecoveryEnvelope's placeholder shape via
composition (RecoveryCapsule WRAPS a WrappedKey, never edits sovereign_identity's own
files). Signed with the owner's real Ed25519 root key (app.sovereign_identity.root_authority)
rather than a second, weaker HMAC scheme -- reuses the one real asymmetric identity
primitive this whole V2 lane already has, instead of inventing a parallel one.
"""

from __future__ import annotations

import hashlib
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.sovereign_identity import EncryptionEnvelope, WrappedKey

from app.life_recovery.types import RecoveryCapsule, RecoveryStateError


def _capsule_signable_payload(
    *,
    capsule_id: uuid.UUID,
    owner_id: uuid.UUID,
    recovery_envelope: WrappedKey,
    encrypted_owner_identity: EncryptionEnvelope,
    wrapped_key_references: tuple[WrappedKey, ...],
    trusted_device_ids: tuple[str, ...],
    critical_config_references: tuple[str, ...],
    life_image_manifest_pointer: str,
    policy_versions: dict[str, int],
    recovery_version: int,
) -> bytes:
    """Canonical byte representation covering every capsule field a tamperer might want to
    alter -- device list, manifest pointer, policy versions, wrapped key references -- not
    just the identifiers. A hash that omitted e.g. trusted_device_ids would let an attacker
    swap in a different device list without breaking integrity, the same "narrow hash" bug
    app.guardian.service._receipt_hash's own docstring warns about."""
    parts = [
        str(capsule_id),
        str(owner_id),
        recovery_envelope.wrapped.hex(),
        recovery_envelope.nonce.hex(),
        encrypted_owner_identity.ciphertext.hex(),
        encrypted_owner_identity.nonce.hex(),
        "|".join(f"{wk.wrapped.hex()}:{wk.nonce.hex()}" for wk in wrapped_key_references),
        ",".join(sorted(trusted_device_ids)),
        ",".join(sorted(critical_config_references)),
        life_image_manifest_pointer,
        ",".join(f"{k}={v}" for k, v in sorted(policy_versions.items())),
        str(recovery_version),
    ]
    return "\x1f".join(parts).encode("utf-8")


def build_recovery_capsule(
    *,
    owner_id: uuid.UUID,
    recovery_envelope: WrappedKey,
    encrypted_owner_identity: EncryptionEnvelope,
    wrapped_key_references: tuple[WrappedKey, ...],
    trusted_device_ids: tuple[str, ...],
    critical_config_references: tuple[str, ...],
    life_image_manifest_pointer: str,
    policy_versions: dict[str, int],
    recovery_version: int,
    owner_root_private_key: bytes,
) -> RecoveryCapsule:
    """`owner_root_private_key` is used only for this one `.sign()` call and never stored
    anywhere in the returned RecoveryCapsule -- mirrors root_authority.sign_challenge()'s
    same discipline (the private key is a caller-held value, this function never persists
    it)."""
    capsule_id = uuid.uuid4()
    payload = _capsule_signable_payload(
        capsule_id=capsule_id,
        owner_id=owner_id,
        recovery_envelope=recovery_envelope,
        encrypted_owner_identity=encrypted_owner_identity,
        wrapped_key_references=wrapped_key_references,
        trusted_device_ids=trusted_device_ids,
        critical_config_references=critical_config_references,
        life_image_manifest_pointer=life_image_manifest_pointer,
        policy_versions=policy_versions,
        recovery_version=recovery_version,
    )
    integrity_hash = hashlib.sha256(payload).hexdigest()
    private_key = Ed25519PrivateKey.from_private_bytes(owner_root_private_key)
    signature = private_key.sign(integrity_hash.encode("utf-8"))
    return RecoveryCapsule(
        capsule_id=capsule_id,
        owner_id=owner_id,
        recovery_envelope=recovery_envelope,
        encrypted_owner_identity=encrypted_owner_identity,
        wrapped_key_references=wrapped_key_references,
        trusted_device_ids=trusted_device_ids,
        critical_config_references=critical_config_references,
        life_image_manifest_pointer=life_image_manifest_pointer,
        policy_versions=policy_versions,
        recovery_version=recovery_version,
        integrity_hash=integrity_hash,
        integrity_signature=signature,
    )


def verify_recovery_capsule_integrity(capsule: RecoveryCapsule, *, owner_root_public_key: bytes) -> bool:
    """Real tamper detection: recompute the hash from the capsule's OWN current field
    values (never trust the stored integrity_hash blindly) and verify the signature over
    that recomputed hash. Returns False (never raises) for a bad signature or a hash that no
    longer matches the recomputed payload -- callers must check the return value, there is
    no silent-pass default."""
    payload = _capsule_signable_payload(
        capsule_id=capsule.capsule_id,
        owner_id=capsule.owner_id,
        recovery_envelope=capsule.recovery_envelope,
        encrypted_owner_identity=capsule.encrypted_owner_identity,
        wrapped_key_references=capsule.wrapped_key_references,
        trusted_device_ids=capsule.trusted_device_ids,
        critical_config_references=capsule.critical_config_references,
        life_image_manifest_pointer=capsule.life_image_manifest_pointer,
        policy_versions=capsule.policy_versions,
        recovery_version=capsule.recovery_version,
    )
    recomputed_hash = hashlib.sha256(payload).hexdigest()
    if recomputed_hash != capsule.integrity_hash:
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(owner_root_public_key)
        public_key.verify(capsule.integrity_signature, capsule.integrity_hash.encode("utf-8"))
        return True
    except InvalidSignature:
        return False


def require_valid_capsule(capsule: RecoveryCapsule, *, owner_root_public_key: bytes) -> None:
    """Raises RecoveryStateError (never a bare False a caller could ignore) if the capsule
    fails integrity verification."""
    if not verify_recovery_capsule_integrity(capsule, owner_root_public_key=owner_root_public_key):
        raise RecoveryStateError(f"recovery capsule {capsule.capsule_id} failed integrity verification")
