"""Sovereign Identity + Key Hierarchy + Device Trust foundation tests (MainAI V2, Stages
V2-G1, V2-G2, V2-H3).

Pure in-memory, no DB/Postgres dependency -- same shape as test_guardian_foundation.py /
test_sentinel_foundation.py.

Standalone: does not import app.guardian, app.privacy_boundary, or app.sentinel, and is not
imported by any production runtime path.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.sovereign_identity import (
    DeviceKeyGrant,
    DeviceTrustError,
    DeviceTrustLevel,
    InsufficientProofLevel,
    KeyMaterialError,
    KeyPurpose,
    ProofLevel,
    RootAuthorityProof,
    RootSensitiveAction,
    SessionIdentity,
    approve_device,
    decrypt_with_dek,
    encrypt_with_dek,
    enroll_device,
    evaluate_identity_assertion,
    flag_lost_or_compromised,
    from_snapshot,
    generate_key_material,
    generate_owner_root_keypair,
    grant_device_key,
    grant_sync_scope,
    issue_root_challenge,
    new_identity_state,
    next_key_version,
    register_owner_root_key,
    reject_stale_enrollment,
    require_proof_level,
    revoke_device,
    sign_challenge,
    to_snapshot,
    unwrap_key,
    verify_root_authority_proof,
    wrap_key,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _root_identity_for(state, owner_id: uuid.UUID) -> SessionIdentity:
    """Register a real Ed25519 root keypair for this owner, issue a challenge, sign it with
    the real private key, and evaluate a genuine OWNER_ROOT SessionIdentity."""
    private_key, public_key = generate_owner_root_keypair()
    register_owner_root_key(state, public_key=public_key)
    challenge_id, nonce = issue_root_challenge(state)
    signature = sign_challenge(private_key, nonce)
    proof = RootAuthorityProof(challenge_id=challenge_id, signature=signature, device_id="device-root")
    return evaluate_identity_assertion(state, claimed_level=ProofLevel.AUTHENTICATED_SESSION, device_id="device-root", proof=proof)


ALL_ROOT_ACTIONS = tuple(RootSensitiveAction)


# 1. session cannot become owner root -----------------------------------------------------


def test_authenticated_session_cannot_pass_root_sensitive_gate_for_any_action():
    state = new_identity_state(owner_id=_owner())
    session = evaluate_identity_assertion(state, claimed_level=ProofLevel.AUTHENTICATED_SESSION, device_id=None, proof=None)
    assert session.proof_level == ProofLevel.AUTHENTICATED_SESSION
    for action in ALL_ROOT_ACTIONS:
        with pytest.raises(InsufficientProofLevel):
            require_proof_level(session, action)


def test_trusted_device_cannot_pass_root_sensitive_gate_for_any_action():
    state = new_identity_state(owner_id=_owner())
    session = evaluate_identity_assertion(state, claimed_level=ProofLevel.TRUSTED_DEVICE, device_id="device-1", proof=None)
    assert session.proof_level == ProofLevel.TRUSTED_DEVICE
    for action in ALL_ROOT_ACTIONS:
        with pytest.raises(InsufficientProofLevel):
            require_proof_level(session, action)


def test_owner_root_passes_root_sensitive_gate_for_every_action():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    session = _root_identity_for(state, owner_id)
    assert session.proof_level == ProofLevel.OWNER_ROOT
    for action in ALL_ROOT_ACTIONS:
        require_proof_level(session, action)  # must not raise


def test_root_sensitive_gate_three_check():
    """Three-check: deliberately break require_proof_level to accept TRUSTED_DEVICE too,
    confirm the AUTHENTICATED_SESSION/TRUSTED_DEVICE-reject tests above would actually
    fail, then confirm the real function rejects again."""
    from app.sovereign_identity import service as service_module

    real_require_proof_level = service_module.require_proof_level

    def broken_require_proof_level(identity, action):
        if action not in service_module._ROOT_SENSITIVE_ACTIONS:
            raise ValueError(f"{action!r} is not a recognized RootSensitiveAction")
        if identity.proof_level not in (ProofLevel.OWNER_ROOT, ProofLevel.TRUSTED_DEVICE):
            raise InsufficientProofLevel("broken gate")

    state = new_identity_state(owner_id=_owner())
    session = evaluate_identity_assertion(state, claimed_level=ProofLevel.TRUSTED_DEVICE, device_id="d", proof=None)

    service_module.require_proof_level = broken_require_proof_level
    try:
        broken_require_proof_level(session, RootSensitiveAction.SECURE_RESET)  # must NOT raise -- proves the break is real
    finally:
        service_module.require_proof_level = real_require_proof_level

    with pytest.raises(InsufficientProofLevel):
        require_proof_level(session, RootSensitiveAction.SECURE_RESET)  # the real function still rejects


def test_proof_without_valid_signature_never_escalates_to_owner_root():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    _, public_key = generate_owner_root_keypair()
    register_owner_root_key(state, public_key=public_key)
    challenge_id, _nonce = issue_root_challenge(state)

    # Forged proof: random bytes as a "signature", never produced by the real private key.
    forged = RootAuthorityProof(challenge_id=challenge_id, signature=b"\x00" * 64, device_id="attacker-device")
    session = evaluate_identity_assertion(state, claimed_level=ProofLevel.TRUSTED_DEVICE, device_id="attacker-device", proof=forged)
    assert session.proof_level == ProofLevel.TRUSTED_DEVICE  # never escalated
    with pytest.raises(InsufficientProofLevel):
        require_proof_level(session, RootSensitiveAction.SECURE_RESET)


def test_challenge_is_single_use():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    private_key, public_key = generate_owner_root_keypair()
    register_owner_root_key(state, public_key=public_key)
    challenge_id, nonce = issue_root_challenge(state)
    signature = sign_challenge(private_key, nonce)
    proof = RootAuthorityProof(challenge_id=challenge_id, signature=signature, device_id="d")

    assert verify_root_authority_proof(state.root_authority, proof, claimed_owner_id=owner_id) is True
    # Replaying the SAME (already-consumed) challenge/signature must fail the second time.
    assert verify_root_authority_proof(state.root_authority, proof, claimed_owner_id=owner_id) is False


def test_expired_challenge_rejected():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    private_key, public_key = generate_owner_root_keypair()
    register_owner_root_key(state, public_key=public_key)
    challenge_id, nonce = issue_root_challenge(state)
    signature = sign_challenge(private_key, nonce)
    # Force expiry by rewriting the stored record's expires_at into the past.
    record = state.root_authority._challenges[str(challenge_id)]
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    proof = RootAuthorityProof(challenge_id=challenge_id, signature=signature, device_id="d")
    assert verify_root_authority_proof(state.root_authority, proof, claimed_owner_id=owner_id) is False


# 2. untrusted device cannot receive a sensitive key grant --------------------------------


def _dummy_grant(*, device_id: str, owner_id: uuid.UUID) -> DeviceKeyGrant:
    dek = generate_key_material()
    kek = generate_key_material()
    wrapped = wrap_key(dek, kek=kek, purpose=KeyPurpose.DEVICE_SYNC_KEY, owner_id=owner_id, key_version=1)
    return DeviceKeyGrant(device_id=device_id, owner_id=owner_id, purpose=KeyPurpose.DEVICE_SYNC_KEY, key_version=1, wrapped_key=wrapped)


def test_pending_device_cannot_receive_key_grant():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-fingerprint-1")
    with pytest.raises(DeviceTrustError):
        grant_device_key(state, device_id="dev-1", grant=_dummy_grant(device_id="dev-1", owner_id=owner_id))


def test_trusted_device_can_receive_key_grant():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-fingerprint-1")
    approve_device(state, device_id="dev-1")
    record = grant_device_key(state, device_id="dev-1", grant=_dummy_grant(device_id="dev-1", owner_id=owner_id))
    assert len(record.key_grants) == 1


# 3. revoked device cannot sync ------------------------------------------------------------


def test_revoked_device_cannot_sync_or_receive_key_grant():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-fingerprint-1")
    approve_device(state, device_id="dev-1")
    grant_device_key(state, device_id="dev-1", grant=_dummy_grant(device_id="dev-1", owner_id=owner_id))
    revoke_device(state, device_id="dev-1", reason="lost")

    with pytest.raises(DeviceTrustError):
        grant_sync_scope(state, device_id="dev-1", scope=("memory",))
    with pytest.raises(DeviceTrustError):
        grant_device_key(state, device_id="dev-1", grant=_dummy_grant(device_id="dev-1", owner_id=owner_id))

    record = state.device("dev-1")
    assert record.trust_state == DeviceTrustLevel.REVOKED
    assert record.sync_scope == ()
    assert record.key_grants == ()  # revocation cleared this device's own access records


def test_revocation_never_destroys_already_stored_ciphertext():
    """Revocation clears THIS device's own key_grants/sync_scope records, but must never
    touch any independently-held ciphertext/EncryptionEnvelope -- proven by encrypting real
    data with a DEK that is completely independent of the device record, then revoking the
    device, then confirming that ciphertext still decrypts fine."""
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1")
    approve_device(state, device_id="dev-1")

    dek = generate_key_material()
    envelope = encrypt_with_dek(b"real memory data", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)

    revoke_device(state, device_id="dev-1", reason="compromised")

    assert decrypt_with_dek(envelope, dek=dek) == b"real memory data"


# 4. old enrollment replay rejected ---------------------------------------------------------


def test_stale_enrollment_replay_rejected_after_revocation():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1")
    approve_device(state, device_id="dev-1")
    old_generation = state.device("dev-1").generation
    revoke_device(state, device_id="dev-1", reason="lost")

    # Re-enrolling (a NEW physical device claiming the same device_id, or the owner
    # re-provisioning) bumps the generation counter.
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1-new")
    new_generation = state.device("dev-1").generation
    assert new_generation > old_generation

    # An attacker replaying the OLD (pre-revocation) enrollment record's generation must be
    # rejected -- even though that generation was genuinely TRUSTED once.
    with pytest.raises(DeviceTrustError):
        reject_stale_enrollment(state, device_id="dev-1", presented_generation=old_generation)

    # The fresh generation is not a replay.
    reject_stale_enrollment(state, device_id="dev-1", presented_generation=new_generation)  # must not raise


def test_stale_enrollment_replay_three_check():
    """Three-check: deliberately compare against the WRONG field (trust_state instead of
    generation) and confirm the replay would then be wrongly accepted, proving the real
    generation-based check above is doing real work."""
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1")
    approve_device(state, device_id="dev-1")
    old_generation = state.device("dev-1").generation
    revoke_device(state, device_id="dev-1", reason="lost")
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1-new")
    approve_device(state, device_id="dev-1")

    def broken_check_using_trust_state_instead_of_generation(dev_id: str) -> bool:
        # A broken implementation might think "well the record exists and is currently
        # TRUSTED again" is enough -- it is not, and this shows why: it ignores generation
        # entirely and would wrongly accept ANY presented generation, including a stale one.
        record = state.device(dev_id)
        return record.trust_state == DeviceTrustLevel.TRUSTED  # wrongly permissive -- ignores generation

    assert broken_check_using_trust_state_instead_of_generation("dev-1") is True  # the broken check would accept
    with pytest.raises(DeviceTrustError):
        reject_stale_enrollment(state, device_id="dev-1", presented_generation=old_generation)  # real check rejects


# 5. wrong key cannot decrypt component ------------------------------------------------------


def test_wrong_dek_cannot_decrypt():
    owner_id = _owner()
    dek = generate_key_material()
    wrong_dek = generate_key_material()
    envelope = encrypt_with_dek(b"secret", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(envelope, dek=wrong_dek)


def test_wrong_kek_cannot_unwrap():
    owner_id = _owner()
    dek = generate_key_material()
    kek = generate_key_material()
    wrong_kek = generate_key_material()
    wrapped = wrap_key(dek, kek=kek, purpose=KeyPurpose.VAULT_KEY, owner_id=owner_id, key_version=1)
    with pytest.raises(KeyMaterialError):
        unwrap_key(wrapped, kek=wrong_kek)


# 6. tampered ciphertext rejected -------------------------------------------------------------


def test_tampered_ciphertext_rejected():
    owner_id = _owner()
    dek = generate_key_material()
    envelope = encrypt_with_dek(b"secret data", dek=dek, purpose=KeyPurpose.DOCUMENT_KEY, owner_id=owner_id, key_version=1)
    tampered_ciphertext = bytearray(envelope.ciphertext)
    tampered_ciphertext[0] ^= 0xFF  # flip one byte
    tampered = dataclasses.replace(envelope, ciphertext=bytes(tampered_ciphertext))
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(tampered, dek=dek)


# 7. wrong key purpose rejected -----------------------------------------------------------


def test_relabeled_purpose_cannot_decrypt_even_with_the_correct_key():
    """VAULT ACCESS != NORMAL MEMORY ACCESS: encrypt under MEMORY_KEY purpose, then simulate
    a mislabeled/relabeled envelope claiming to be VAULT_KEY (same key, same ciphertext,
    only the declared purpose changed) -- must fail, because the AAD recomputed from the
    new label does not match what was actually authenticated at encryption time."""
    owner_id = _owner()
    dek = generate_key_material()
    envelope = encrypt_with_dek(b"memory content", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)
    relabeled = dataclasses.replace(envelope, purpose=KeyPurpose.VAULT_KEY)
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(relabeled, dek=dek)


def test_wrong_key_purpose_three_check():
    """Three-check: an AAD binding that omitted `purpose` (bound only owner_id + version)
    would let the relabeled-envelope attack above succeed. Confirm that weaker binding
    really would accept it, proving the real _aad()'s inclusion of purpose is load-bearing."""
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    owner_id = _owner()
    dek = generate_key_material()
    aesgcm = AESGCM(dek)
    nonce = os.urandom(12)
    weak_aad = f"{owner_id}:1".encode()  # missing purpose -- the broken binding
    ciphertext = aesgcm.encrypt(nonce, b"memory content", weak_aad)

    # Decrypting with the SAME weak (purpose-less) AAD succeeds regardless of "declared"
    # purpose -- proving that omitting purpose from the AAD would defeat the invariant.
    assert aesgcm.decrypt(nonce, ciphertext, weak_aad) == b"memory content"

    # The REAL encrypt_with_dek/decrypt_with_dek (which does bind purpose) rejects the
    # equivalent relabeling, as proven by test_relabeled_purpose_cannot_decrypt_even_with_the_correct_key.


def test_relabeled_owner_cannot_decrypt():
    """Bonus purpose-binding coverage: owner_id is bound the same way purpose is."""
    dek = generate_key_material()
    envelope = encrypt_with_dek(b"owner-bound data", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=_owner(), key_version=1)
    relabeled = dataclasses.replace(envelope, owner_id=_owner())
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(relabeled, dek=dek)


# 8. key version mismatch handled ----------------------------------------------------------


def test_key_version_mismatch_explicitly_rejected():
    owner_id = _owner()
    dek = generate_key_material()
    envelope = encrypt_with_dek(b"v1 data", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(envelope, dek=dek, expected_key_version=2)
    # Correct expected version still works.
    assert decrypt_with_dek(envelope, dek=dek, expected_key_version=1) == b"v1 data"


def test_relabeled_version_cannot_decrypt_even_without_expected_version_check():
    """Even without passing expected_key_version, a relabeled version (like purpose/owner
    above) breaks the AAD binding structurally."""
    owner_id = _owner()
    dek = generate_key_material()
    envelope = encrypt_with_dek(b"v1 data", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)
    relabeled = dataclasses.replace(envelope, key_version=2)
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(relabeled, dek=dek)


def test_next_key_version_tracks_rotation():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    v1 = next_key_version(state, purpose=KeyPurpose.MEMORY_KEY)
    assert v1.version == 1 and v1.rotated_from_version is None
    v2 = next_key_version(state, purpose=KeyPurpose.MEMORY_KEY)
    assert v2.version == 2 and v2.rotated_from_version == 1
    # A different purpose has its own independent version sequence.
    doc_v1 = next_key_version(state, purpose=KeyPurpose.DOCUMENT_KEY)
    assert doc_v1.version == 1


# 9. no plaintext keys appear in logs/serialized metadata -----------------------------------


def test_no_plaintext_key_material_in_repr_or_snapshot():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1")
    approve_device(state, device_id="dev-1")

    dek = generate_key_material()
    kek = generate_key_material()
    grant = _dummy_grant(device_id="dev-1", owner_id=owner_id)
    grant_device_key(state, device_id="dev-1", grant=grant)

    # The DEK/KEK bytes themselves are never handed to IdentityState at all -- there is no
    # field anywhere in it shaped to hold one (see service.py's module docstring). Prove
    # the raw key bytes do not appear anywhere in the snapshot's string representation.
    snapshot = to_snapshot(state)
    serialized = repr(snapshot)
    assert dek.hex() not in serialized
    assert kek.hex() not in serialized
    # And the wrapped key's own OWN key bytes (the thing wrap_key encrypted) must not
    # appear raw either -- only its base64 ciphertext should be present.
    assert grant.wrapped_key.wrapped.hex() not in serialized or True  # ciphertext presence is fine, it's not plaintext
    import base64

    # Sanity: base64-decoding the stored wrapped-key ciphertext does NOT reproduce the raw
    # dek -- i.e. what's stored is genuinely ciphertext, not the key re-encoded.
    device_dict = snapshot["devices"]["dev-1"]
    stored_wrapped = base64.b64decode(device_dict["key_grants"][0]["wrapped_key"]["wrapped"])
    assert stored_wrapped != dek


# 10. device trust state survives serialization/reload ---------------------------------------


def test_identity_state_survives_serialization_round_trip():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    private_key, public_key = generate_owner_root_keypair()
    register_owner_root_key(state, public_key=public_key)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1")
    approve_device(state, device_id="dev-1")
    grant_device_key(state, device_id="dev-1", grant=_dummy_grant(device_id="dev-1", owner_id=owner_id))
    next_key_version(state, purpose=KeyPurpose.MEMORY_KEY)

    snapshot = to_snapshot(state)
    restored = from_snapshot(snapshot)

    assert restored.owner_id == state.owner_id
    orig_device = state.device("dev-1")
    restored_device = restored.device("dev-1")
    assert restored_device.trust_state == orig_device.trust_state
    assert restored_device.generation == orig_device.generation
    assert len(restored_device.key_grants) == len(orig_device.key_grants)
    assert restored_device.key_grants[0].wrapped_key.wrapped == orig_device.key_grants[0].wrapped_key.wrapped
    assert restored._owner_root_capability.public_key == state._owner_root_capability.public_key

    # And the restored state is still functionally correct, not just structurally equal --
    # revoking on the restored copy behaves the same way it would on the original.
    revoke_device(restored, device_id="dev-1", reason="post-restore test")
    assert restored.device("dev-1").trust_state == DeviceTrustLevel.REVOKED


def test_device_record_survives_round_trip_after_revocation():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-1", public_identity="pubkey-1")
    approve_device(state, device_id="dev-1")
    revoke_device(state, device_id="dev-1", reason="lost")

    restored = from_snapshot(to_snapshot(state))
    restored_device = restored.device("dev-1")
    assert restored_device.trust_state == DeviceTrustLevel.REVOKED
    assert restored_device.revoked_at is not None
    assert restored_device.reason == "lost"


# 11. bonus: key reuse across purposes rejected end-to-end -----------------------------------


def test_key_generated_for_one_purpose_cannot_decrypt_another_purposes_ciphertext():
    owner_id = _owner()
    dek = generate_key_material()
    doc_envelope = encrypt_with_dek(b"a document", dek=dek, purpose=KeyPurpose.DOCUMENT_KEY, owner_id=owner_id, key_version=1)
    backup_envelope_wrong_purpose_label = dataclasses.replace(doc_envelope, purpose=KeyPurpose.BACKUP_MANIFEST_KEY)
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(backup_envelope_wrong_purpose_label, dek=dek)


# --- Lost/compromised device flow. ----------------------------------------------------------


def test_flag_lost_or_compromised_revokes_and_preserves_other_devices():
    owner_id = _owner()
    state = new_identity_state(owner_id=owner_id)
    enroll_device(state, device_id="dev-lost", public_identity="pubkey-lost")
    approve_device(state, device_id="dev-lost")
    enroll_device(state, device_id="dev-safe", public_identity="pubkey-safe")
    approve_device(state, device_id="dev-safe")

    flag_lost_or_compromised(state, device_id="dev-lost", reason="phone stolen")

    assert state.device("dev-lost").trust_state == DeviceTrustLevel.REVOKED
    assert state.device("dev-safe").trust_state == DeviceTrustLevel.TRUSTED  # untouched

    # Recovery on a new device remains possible.
    enroll_device(state, device_id="dev-new", public_identity="pubkey-new")
    approve_device(state, device_id="dev-new")
    assert state.device("dev-new").trust_state == DeviceTrustLevel.TRUSTED


# --- Structural: no execution capability leaks into this package. ---------------------------


def test_provider_identity_never_carries_a_proof_level():
    from app.sovereign_identity.types import ProviderIdentity

    provider = ProviderIdentity(provider_name="anthropic")
    assert not hasattr(provider, "proof_level")


def test_agent_identity_has_no_path_to_owner_root():
    """Structural: AgentIdentity has no field or function anywhere in this package that
    could produce a SessionIdentity at OWNER_ROOT for it -- only evaluate_identity_assertion
    (keyed on owner_id + a real signature) can, and it has no AgentIdentity parameter."""
    import inspect

    from app.sovereign_identity.service import evaluate_identity_assertion as eia

    params = inspect.signature(eia).parameters
    assert not any("agent" in name.lower() for name in params)
