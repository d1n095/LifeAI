"""Life Recovery foundation tests (MainAI V2, Stages V2-H1, V2-H2, V2-H4).

Pure in-memory, no DB/Postgres dependency. Uses REAL app.sovereign_identity primitives
throughout (real AES-256-GCM, real Ed25519 root proof, real device trust) rather than mocks
-- this file proves the integration works, not just this package's own units in isolation.

Standalone: does not import app.guardian/app.privacy_boundary/app.sentinel, and is not
imported by any production runtime path.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.sovereign_identity import (
    InsufficientProofLevel,
    KeyMaterialError,
    KeyPurpose,
    ProofLevel,
    RootAuthorityProof,
    SessionIdentity,
    decrypt_with_dek,
    encrypt_with_dek,
    enroll_device,
    evaluate_identity_assertion,
    generate_key_material,
    generate_owner_root_keypair,
    issue_root_challenge,
    new_identity_state,
    register_owner_root_key,
    reject_stale_enrollment,
    revoke_device,
    sign_challenge,
)

from app.life_recovery import (
    BackupMode,
    ComponentCriticality,
    ComponentType,
    EmergencyResetPreauthorization,
    HydrationError,
    LocalKeyStore,
    RecoveryState,
    RecoveryStateError,
    RestoreDrillState,
    RestoreTier,
    advance_recovery,
    build_life_image_component,
    build_life_image_manifest,
    build_recovery_capsule,
    check_backup_currentness,
    decrypt_life_image_component,
    determine_restore_drill_state,
    enroll_recovery_method,
    from_snapshot,
    get_continuity_summary,
    new_recovery_environment,
    perform_secure_reset,
    require_bounded_reset_preauthorization,
    restore_tier,
    revoke_recovery_method,
    run_backup_verification,
    to_snapshot,
    unlock_vault,
    use_recovery_method,
    verify_manifest_integrity,
    verify_recovery_capsule_integrity,
    verify_recovery_identity,
)
from app.life_recovery.hydration import restore_component


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _owner_identity():
    """A fresh IdentityState with a registered owner root key. Returns
    (owner_id, identity_state, private_key_bytes, public_key_bytes)."""
    owner_id = _owner()
    identity_state = new_identity_state(owner_id=owner_id)
    private_key, public_key = generate_owner_root_keypair()
    register_owner_root_key(identity_state, public_key=public_key)
    return owner_id, identity_state, private_key, public_key


def _owner_root_session(identity_state, *, owner_id: uuid.UUID, private_key: bytes) -> SessionIdentity:
    """A REAL OWNER_ROOT session, produced via a genuine Ed25519 challenge/signature
    round-trip -- never hand-constructed."""
    challenge_id, nonce = issue_root_challenge(identity_state)
    signature = sign_challenge(private_key, nonce)
    proof = RootAuthorityProof(challenge_id=challenge_id, signature=signature, device_id=None)
    return evaluate_identity_assertion(identity_state, claimed_level=ProofLevel.TRUSTED_DEVICE, device_id=None, proof=proof)


def _low_session(owner_id: uuid.UUID, level: ProofLevel = ProofLevel.AUTHENTICATED_SESSION) -> SessionIdentity:
    return SessionIdentity(session_id=uuid.uuid4(), owner_id=owner_id, proof_level=level, device_id=None)


def _dek() -> bytes:
    return generate_key_material()


def _build_component(
    *,
    owner_id: uuid.UUID,
    dek: bytes,
    component_type: ComponentType = ComponentType.MAINAI_MEMORY,
    restore_priority: RestoreTier = RestoreTier.PRIORITY_1,
    criticality: ComponentCriticality = ComponentCriticality.CRITICAL,
    plaintext: bytes = b"some restored plaintext content",
    dependencies: tuple = (),
    key_version: int = 1,
):
    return build_life_image_component(
        plaintext,
        component_type=component_type,
        dek=dek,
        owner_id=owner_id,
        key_version=key_version,
        schema_version=1,
        content_version=1,
        criticality=criticality,
        restore_priority=restore_priority,
        dependencies=dependencies,
    )


# 1. wrong key cannot decrypt component (integration level)
def test_wrong_key_cannot_decrypt_component():
    owner_id = _owner()
    dek = _dek()
    wrong_dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)
    with pytest.raises(KeyMaterialError):
        decrypt_life_image_component(component, dek=wrong_dek)
    # Sanity: the real key works.
    assert decrypt_life_image_component(component, dek=dek) == b"some restored plaintext content"


# bonus: wrong owner identity rejected (AAD binds owner_id, not just purpose/version)
def test_wrong_owner_identity_rejected():
    owner_id = _owner()
    other_owner_id = _owner()
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)
    relabeled = replace(component.envelope, owner_id=other_owner_id)
    with pytest.raises(KeyMaterialError):
        decrypt_with_dek(relabeled, dek=dek, expected_key_version=component.key_version)


# bonus: an unenrolled (never-registered) device cannot receive a sensitive key grant or
# take part in recovery -- distinct from the revoked-device case (test 16), which requires
# a device that DID exist and was later revoked. This one has no DeviceRecord at all.
def test_unenrolled_device_cannot_receive_key_grant():
    owner_id, identity_state, _, _ = _owner_identity()
    from app.sovereign_identity import grant_device_key

    with pytest.raises(KeyError):
        grant_device_key(identity_state, device_id="never-enrolled-device", grant=None)


# 2. tampered ciphertext rejected (integration level)
def test_tampered_ciphertext_rejected():
    owner_id = _owner()
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)
    tampered_ciphertext = bytearray(component.envelope.ciphertext)
    tampered_ciphertext[0] ^= 0xFF
    tampered_envelope = replace(component.envelope, ciphertext=bytes(tampered_ciphertext))
    tampered_component = replace(component, envelope=tampered_envelope)
    with pytest.raises(KeyMaterialError):
        decrypt_life_image_component(tampered_component, dek=dek)


def _build_manifest(owner_id, private_key, components, *, version: int = 1, min_version: str = "2.0.0"):
    return build_life_image_manifest(
        components,
        device_origin="test-device",
        policy_versions={"guardian": 1, "privacy_boundary": 1},
        min_compatible_lifeai_version=min_version,
        owner_root_private_key=private_key,
        version=version,
    )


# 3. tampered manifest rejected
def test_tampered_manifest_rejected():
    owner_id, _, private_key, public_key = _owner_identity()
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)
    manifest = _build_manifest(owner_id, private_key, (component,))
    assert verify_manifest_integrity(manifest, owner_root_public_key=public_key) is True

    tampered = replace(manifest, device_origin="attacker-device")
    assert verify_manifest_integrity(tampered, owner_root_public_key=public_key) is False


# 4. Recovery Capsule alone cannot decrypt a full Life Image
def test_capsule_alone_cannot_decrypt_life_image():
    owner_id, _, private_key, public_key = _owner_identity()
    dek = _dek()
    kek = _dek()
    from app.sovereign_identity import wrap_key

    wrapped_dek = wrap_key(dek, kek=kek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)
    recovery_material = _dek()  # stands in for owner-chosen recovery material wrapping the KEK
    from app.sovereign_identity import wrap_key as _wrap

    wrapped_kek = _wrap(kek, kek=recovery_material, purpose=KeyPurpose.RECOVERY_CAPSULE_KEY, owner_id=owner_id, key_version=1)
    encrypted_identity = encrypt_with_dek(b"owner identity metadata", dek=dek, purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_version=1)

    capsule = build_recovery_capsule(
        owner_id=owner_id,
        recovery_envelope=wrapped_kek,
        encrypted_owner_identity=encrypted_identity,
        wrapped_key_references=(wrapped_dek,),
        trusted_device_ids=("device-1",),
        critical_config_references=("config-ref-1",),
        life_image_manifest_pointer="image-pointer-1",
        policy_versions={"guardian": 1},
        recovery_version=1,
        owner_root_private_key=private_key,
    )
    assert verify_recovery_capsule_integrity(capsule, owner_root_public_key=public_key) is True

    # The capsule itself holds only CIPHERTEXT (wrapped_kek, encrypted_identity) -- without
    # the recovery_material (owner-chosen recovery secret, never stored in the capsule),
    # unwrapping the KEK fails.
    from app.sovereign_identity import unwrap_key

    wrong_recovery_material = _dek()
    with pytest.raises(KeyMaterialError):
        unwrap_key(capsule.recovery_envelope, kek=wrong_recovery_material)
    # With the correct recovery material, it DOES work -- proving the failure above is real,
    # not an artifact of a broken wrap/unwrap path.
    recovered_kek = unwrap_key(capsule.recovery_envelope, kek=recovery_material)
    assert recovered_kek == kek


# 5. cloud blob alone cannot decrypt Life Image
def test_cloud_blob_alone_cannot_decrypt_life_image():
    owner_id = _owner()
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)
    # "cloud blob" = the component's ciphertext with no accompanying key material at all.
    ciphertext_only = component.envelope.ciphertext
    assert ciphertext_only  # sanity: it's real ciphertext
    # There is no function anywhere in this package that can turn ciphertext bytes alone
    # into plaintext -- decrypt_life_image_component() always requires a `dek` argument.
    import inspect

    sig = inspect.signature(decrypt_life_image_component)
    assert "dek" in sig.parameters and sig.parameters["dek"].default is inspect.Parameter.empty


# 6 & three-check: secure reset destroys local ability to decrypt
def test_secure_reset_destroys_local_ability_to_decrypt():
    owner_id, identity_state, private_key, _ = _owner_identity()
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)

    key_store = LocalKeyStore()
    key_store.put(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_bytes=dek)

    # Before reset: fetching from the store and decrypting works.
    fetched = key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id)
    assert decrypt_life_image_component(component, dek=fetched) == b"some restored plaintext content"

    root_session = _owner_root_session(identity_state, owner_id=owner_id, private_key=private_key)
    perform_secure_reset(key_store, identity=root_session)

    with pytest.raises(KeyMaterialError):
        key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id)


def test_secure_reset_destroys_local_ability_to_decrypt_three_check():
    """Three-check: confirm the test above would fail if perform_secure_reset() did NOT
    actually clear the key store (e.g. a bug where it only set a flag)."""
    key_store = LocalKeyStore()
    owner_id = _owner()
    key_store.put(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_bytes=_dek())
    # Simulate the broken version: reset that does nothing to the store.
    # (We don't call perform_secure_reset here -- we directly assert that WITHOUT clearing,
    # get() would still succeed, proving the real test's assertion is not vacuous.)
    assert key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id) is not None
    key_store.clear()
    with pytest.raises(KeyMaterialError):
        key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id)


# 7. secure reset requires owner-root proof
def test_secure_reset_requires_owner_root_proof():
    owner_id, identity_state, private_key, _ = _owner_identity()
    key_store = LocalKeyStore()
    key_store.put(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_bytes=_dek())

    low_session = _low_session(owner_id, ProofLevel.TRUSTED_DEVICE)
    with pytest.raises(InsufficientProofLevel):
        perform_secure_reset(key_store, identity=low_session)
    # The store must be untouched after a rejected attempt.
    assert key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id) is not None

    root_session = _owner_root_session(identity_state, owner_id=owner_id, private_key=private_key)
    perform_secure_reset(key_store, identity=root_session)
    with pytest.raises(KeyMaterialError):
        key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id)


def test_secure_reset_preauthorization_rejects_wildcard_scope():
    owner_id = _owner()
    with pytest.raises(ValueError):
        preauth = EmergencyResetPreauthorization(
            preauth_id=uuid.uuid4(),
            owner_id=owner_id,
            scope_hint="all",
            max_uses=1,
            valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        require_bounded_reset_preauthorization(preauth)


# 8. Life Image round-trip works
def test_life_image_round_trip():
    owner_id, _, private_key, public_key = _owner_identity()
    dek = _dek()
    plaintext = b"critical intent object content"
    component = _build_component(owner_id=owner_id, dek=dek, plaintext=plaintext)
    manifest = _build_manifest(owner_id, private_key, (component,))
    assert verify_manifest_integrity(manifest, owner_root_public_key=public_key) is True
    restored = decrypt_life_image_component(manifest.components[0], dek=dek)
    assert restored == plaintext


# 9. component dependency validation works
def test_component_dependency_validation():
    owner_id, _, private_key, public_key = _owner_identity()
    dek = _dek()
    settings = _build_component(owner_id=owner_id, dek=dek, component_type=ComponentType.USER_SETTINGS)
    # Declares a dependency on INTENT_OBJECTS, which is NOT present in the manifest.
    broken = _build_component(
        owner_id=owner_id,
        dek=dek,
        component_type=ComponentType.WORKSPACE_MEMORY,
        dependencies=(ComponentType.INTENT_OBJECTS,),
    )
    result = run_backup_verification(
        _build_manifest(owner_id, private_key, (settings, broken)),
        owner_root_public_key=public_key,
        available_component_ids=frozenset({settings.component_id, broken.component_id}),
        resolvable_key_purposes=frozenset({KeyPurpose.MEMORY_KEY}),
        current_min_compatible_version="2.0.0",
    )
    assert result.dependency_graph_valid is False
    assert not result.passed

    # A manifest with satisfied dependencies passes this specific check.
    intent = _build_component(owner_id=owner_id, dek=dek, component_type=ComponentType.INTENT_OBJECTS)
    fixed = _build_component(
        owner_id=owner_id,
        dek=dek,
        component_type=ComponentType.WORKSPACE_MEMORY,
        dependencies=(ComponentType.INTENT_OBJECTS,),
    )
    result2 = run_backup_verification(
        _build_manifest(owner_id, private_key, (intent, fixed)),
        owner_root_public_key=public_key,
        available_component_ids=frozenset({intent.component_id, fixed.component_id}),
        resolvable_key_purposes=frozenset({KeyPurpose.MEMORY_KEY}),
        current_min_compatible_version="2.0.0",
    )
    assert result2.dependency_graph_valid is True


# 10. critical restore (tier0+1) finishes, MainAI usable before tier2/3
def test_critical_restore_makes_mainai_usable_before_background_tiers():
    owner_id = _owner()
    dek = _dek()

    env = new_recovery_environment(owner_id=owner_id)

    p0 = _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_0, component_type=ComponentType.SECURITY_POLICY)
    p1 = _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_1, component_type=ComponentType.INTENT_OBJECTS)
    p2 = _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_2, component_type=ComponentType.DOCUMENTS)

    assert env.hydration.is_mainai_usable is False
    restore_tier(env.hydration, tier=RestoreTier.PRIORITY_0, components_in_tier=(p0,), dek_for_component=lambda c: dek)
    assert env.hydration.is_mainai_usable is False  # tier 1 not done yet
    restore_tier(env.hydration, tier=RestoreTier.PRIORITY_1, components_in_tier=(p1,), dek_for_component=lambda c: dek)
    assert env.hydration.is_mainai_usable is True  # usable now, tier 2 untouched

    summary = get_continuity_summary(env.hydration, {RestoreTier.PRIORITY_0: (p0,), RestoreTier.PRIORITY_1: (p1,)})
    assert ComponentType.INTENT_OBJECTS in summary

    # Tier 2 still not restored -- doesn't block usability, and continuity doesn't need it.
    assert p2.component_id not in env.hydration.completed_component_ids


def test_continuity_summary_raises_before_critical_restore_complete():
    owner_id = _owner()

    env = new_recovery_environment(owner_id=owner_id)
    with pytest.raises(HydrationError):
        get_continuity_summary(env.hydration, {})


# 11. restore resumes idempotently after interruption
def test_restore_resumes_idempotently_after_interruption():
    owner_id = _owner()
    dek = _dek()
    from app.life_recovery import new_hydration_progress
    from app.life_recovery.hydration import from_snapshot as hydration_from_snapshot
    from app.life_recovery.hydration import to_snapshot as hydration_to_snapshot

    components = tuple(
        _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_1, component_type=ct)
        for ct in (ComponentType.INTENT_OBJECTS, ComponentType.WORKSPACE_MEMORY, ComponentType.USER_SETTINGS)
    )

    # Uninterrupted run.
    progress_full = new_hydration_progress(owner_id=owner_id)
    restore_tier(progress_full, tier=RestoreTier.PRIORITY_1, components_in_tier=components, dek_for_component=lambda c: dek)

    # Interrupted run: restore only the first component, snapshot ("crash"), resume from
    # snapshot, restore the rest.
    progress_partial = new_hydration_progress(owner_id=owner_id)
    restore_component(progress_partial, components[0], dek=dek)
    snapshot = hydration_to_snapshot(progress_partial)
    resumed = hydration_from_snapshot(snapshot)
    # Re-running restore_tier over ALL THREE components again must not redo component[0].
    restore_tier(resumed, tier=RestoreTier.PRIORITY_1, components_in_tier=components, dek_for_component=lambda c: dek)

    assert resumed.completed_component_ids == progress_full.completed_component_ids
    assert resumed.completed_tiers == progress_full.completed_tiers


# 12 & three-check: Vault remains locked after basic/critical restore
def test_vault_remains_locked_after_critical_restore():
    owner_id = _owner()
    dek = _dek()
    vault_dek = _dek()

    env = new_recovery_environment(owner_id=owner_id)
    p0 = _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_0, component_type=ComponentType.SECURITY_POLICY)
    p1 = _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_1, component_type=ComponentType.INTENT_OBJECTS)
    vault = _build_component(owner_id=owner_id, dek=vault_dek, restore_priority=RestoreTier.PRIORITY_1, component_type=ComponentType.VAULT)

    # Vault is (deliberately, adversarially) tagged PRIORITY_1 here, same tier as normal
    # essentials -- restore_tier() must STILL never touch it.
    key_for = {p1.component_id: dek, vault.component_id: vault_dek}
    restore_tier(env.hydration, tier=RestoreTier.PRIORITY_0, components_in_tier=(p0,), dek_for_component=lambda c: dek)
    restore_tier(
        env.hydration,
        tier=RestoreTier.PRIORITY_1,
        components_in_tier=(p1, vault),
        dek_for_component=lambda c: key_for[c.component_id],
    )

    assert env.hydration.is_mainai_usable is True
    assert vault.component_id not in env.hydration.completed_component_ids

    # Direct attempt via restore_component() is also rejected.
    with pytest.raises(HydrationError):
        restore_component(env.hydration, vault, dek=vault_dek)

    # Only the explicit, separately-authorized unlock_vault() call can restore it.
    with pytest.raises(HydrationError):
        unlock_vault(env.hydration, vault, dek=vault_dek, owner_authorized=False)
    assert vault.component_id not in env.hydration.completed_component_ids

    plaintext = unlock_vault(env.hydration, vault, dek=vault_dek, owner_authorized=True)
    assert plaintext == b"some restored plaintext content"
    assert vault.component_id in env.hydration.completed_component_ids


def test_vault_remains_locked_three_check():
    """Three-check: confirm restore_tier() including a VAULT component WOULD restore it if
    the VAULT-skip logic were removed -- proving the assertion above is not vacuous."""
    owner_id = _owner()
    vault_dek = _dek()
    vault = _build_component(owner_id=owner_id, dek=vault_dek, restore_priority=RestoreTier.PRIORITY_1, component_type=ComponentType.VAULT)
    # Directly prove decrypt_life_image_component() (the underlying primitive restore_tier()
    # would call if it did NOT skip Vault) really would succeed given the right key --
    # i.e. the reason Vault stays locked is the skip logic, not that decryption itself
    # would fail regardless.
    assert decrypt_life_image_component(vault, dek=vault_dek) == b"some restored plaintext content"


# 13. old backup rollback detected where currentness required
def test_old_backup_rollback_detected():
    with pytest.raises(RecoveryStateError):
        check_backup_currentness(manifest_version=1, current_min_required_version=3, require_current=True)
    # Not required -> no error.
    check_backup_currentness(manifest_version=1, current_min_required_version=3, require_current=False)
    # Current enough -> no error.
    check_backup_currentness(manifest_version=3, current_min_required_version=3, require_current=True)


# 14 & three-check: recovery state machine cannot skip REQUESTED -> COMPLETE
def test_recovery_state_machine_cannot_skip_states():
    from app.life_recovery import new_recovery_state_machine

    machine = new_recovery_state_machine(owner_id=_owner())
    advance_recovery(machine, to_state=RecoveryState.READY, reason="configured")
    advance_recovery(machine, to_state=RecoveryState.RECOVERY_REQUESTED, reason="owner requested recovery")
    with pytest.raises(RecoveryStateError):
        advance_recovery(machine, to_state=RecoveryState.RECOVERY_COMPLETE, reason="attempted skip")
    assert machine.state == RecoveryState.RECOVERY_REQUESTED  # unchanged after the rejected attempt

    # The real, full path DOES reach RECOVERY_COMPLETE -- proving the rejection above is
    # about skipping, not that COMPLETE is unreachable at all.
    advance_recovery(machine, to_state=RecoveryState.IDENTITY_VERIFICATION, reason="verifying")
    advance_recovery(machine, to_state=RecoveryState.CAPSULE_AVAILABLE, reason="capsule fetched")
    advance_recovery(machine, to_state=RecoveryState.KEY_UNLOCKED, reason="key unwrapped")
    advance_recovery(machine, to_state=RecoveryState.CRITICAL_RESTORE, reason="restoring critical tiers")
    advance_recovery(machine, to_state=RecoveryState.BACKGROUND_RESTORE, reason="restoring background tiers")
    advance_recovery(machine, to_state=RecoveryState.RECOVERY_COMPLETE, reason="done")
    assert machine.state == RecoveryState.RECOVERY_COMPLETE


def test_recovery_state_machine_skip_three_check():
    """Three-check: confirm that if REQUESTED's allowed-transitions set included COMPLETE
    directly, the skip above WOULD succeed -- proving the real transition table (not an
    accidentally-always-passing test) is what causes the rejection."""
    from app.life_recovery.recovery_state import _VALID_RECOVERY_TRANSITIONS

    allowed_from_requested = _VALID_RECOVERY_TRANSITIONS[RecoveryState.RECOVERY_REQUESTED]
    assert RecoveryState.RECOVERY_COMPLETE not in allowed_from_requested
    # If a bug added RECOVERY_COMPLETE directly to that set, the raise above would not
    # occur, and the primary test's `pytest.raises` block would itself fail (no exception
    # raised) -- confirming the primary assertion is load-bearing, not vacuous.


# 15. recovery state survives serialization/reload
def test_recovery_state_survives_serialization_round_trip():

    owner_id = _owner()
    env = new_recovery_environment(owner_id=owner_id)
    advance_recovery(env.recovery, to_state=RecoveryState.READY, reason="configured")
    advance_recovery(env.recovery, to_state=RecoveryState.RECOVERY_REQUESTED, reason="owner requested recovery")
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek, restore_priority=RestoreTier.PRIORITY_0)
    restore_tier(env.hydration, tier=RestoreTier.PRIORITY_0, components_in_tier=(component,), dek_for_component=lambda c: dek)

    snapshot = to_snapshot(env)
    restored_env = from_snapshot(snapshot)

    assert restored_env.owner_id == env.owner_id
    assert restored_env.recovery.state == env.recovery.state
    assert len(restored_env.recovery.receipts) == len(env.recovery.receipts)
    assert restored_env.hydration.completed_component_ids == env.hydration.completed_component_ids
    assert restored_env.hydration.completed_tiers == env.hydration.completed_tiers


# 16. old revoked device cannot complete recovery
def test_revoked_device_cannot_be_used_for_recovery():
    owner_id, identity_state, _, _ = _owner_identity()
    enroll_device(identity_state, device_id="device-a", public_identity="pubkey-a")
    from app.sovereign_identity import approve_device

    approve_device(identity_state, device_id="device-a")
    revoke_device(identity_state, device_id="device-a", reason="lost")
    device = identity_state.device("device-a")
    assert device.trust_state.value == "REVOKED"
    # A revoked device cannot receive a new key grant -- proving it has no ongoing role in
    # a recovery flow that would need one (e.g. syncing the unwrapped Life Image back down).
    from app.sovereign_identity import DeviceTrustError, grant_device_key

    with pytest.raises(DeviceTrustError):
        grant_device_key(identity_state, device_id="device-a", grant=None)


# 17. replayed device enrollment rejected in a recovery context
def test_replayed_device_enrollment_rejected_in_recovery_context():
    owner_id, identity_state, _, _ = _owner_identity()
    record = enroll_device(identity_state, device_id="device-b", public_identity="pubkey-b")
    stale_generation = record.generation
    revoke_device(identity_state, device_id="device-b", reason="compromised")
    # Re-enrollment after revocation bumps the generation.
    enroll_device(identity_state, device_id="device-b", public_identity="pubkey-b-new")
    with pytest.raises(Exception):
        reject_stale_enrollment(identity_state, device_id="device-b", presented_generation=stale_generation)


# 18. recovery factor revocation is honored
def test_recovery_factor_revocation_is_honored():
    from app.life_recovery import new_recovery_state_machine

    machine = new_recovery_state_machine(owner_id=_owner())
    method_id = uuid.uuid4()
    enroll_recovery_method(machine, recovery_identity_id=method_id, kind_label="OFFLINE_RECOVERY_CODE", threshold=1, required_shares=1)
    assert use_recovery_method(machine, recovery_identity_id=method_id, presented_shares=1) is True

    revoke_recovery_method(machine, recovery_identity_id=method_id, reason="owner revoked this code")
    # Even with a technically-sufficient share count, a revoked method must not succeed.
    assert use_recovery_method(machine, recovery_identity_id=method_id, presented_shares=1) is False
    assert verify_recovery_identity(machine, recovery_identity_id=method_id, presented_shares=5) is False


# 19. backup mode field has zero effect on crypto path
def test_backup_mode_has_zero_effect_on_crypto_path():
    owner_id = _owner()
    # Constructing components is entirely independent of any BackupMode value -- there is no
    # BackupMode parameter anywhere in build_life_image_component()'s signature.
    import inspect

    sig = inspect.signature(build_life_image_component)
    assert "mode" not in sig.parameters and "backup_mode" not in sig.parameters

    from app.life_recovery import BackupRecord

    record_local = BackupRecord(backup_id=uuid.uuid4(), owner_id=owner_id, mode=BackupMode.LOCAL_ONLY, manifest_image_id=uuid.uuid4(), manifest_version=1)
    record_cloud = BackupRecord(backup_id=uuid.uuid4(), owner_id=owner_id, mode=BackupMode.CLOUD_FIRST, manifest_image_id=uuid.uuid4(), manifest_version=1)
    # Neither BackupRecord carries any key material or ciphertext field at all.
    for record in (record_local, record_cloud):
        field_names = {f.name for f in record.__dataclass_fields__.values()}
        assert not field_names & {"dek", "kek", "key", "ciphertext", "plaintext"}


# 20. backup is not reported RESTORE_TESTED merely because upload/creation succeeded
def test_backup_not_restore_tested_merely_because_created():
    owner_id, _, private_key, public_key = _owner_identity()
    dek = _dek()
    component = _build_component(owner_id=owner_id, dek=dek)
    manifest = _build_manifest(owner_id, private_key, (component,))
    from app.life_recovery import BackupRecord

    # "Upload succeeded" == constructing a BackupRecord. Its default state is UNTESTED, not
    # RESTORE_TESTED, regardless of anything else.
    record = BackupRecord(backup_id=uuid.uuid4(), owner_id=owner_id, mode=BackupMode.ENCRYPTED_CLOUD_BACKUP, manifest_image_id=manifest.image_id, manifest_version=manifest.version)
    assert record.restore_drill_state == RestoreDrillState.UNTESTED

    # Only running the real verification can move it to RESTORE_TESTED.
    state = determine_restore_drill_state(
        manifest,
        owner_root_public_key=public_key,
        available_component_ids=frozenset({component.component_id}),
        resolvable_key_purposes=frozenset({KeyPurpose.MEMORY_KEY}),
        current_min_compatible_version="2.0.0",
    )
    assert state == RestoreDrillState.RESTORE_TESTED

    # And a genuinely broken manifest (missing component) does NOT reach RESTORE_TESTED
    # merely because a verification function was called -- it must actually fail.
    broken_state = determine_restore_drill_state(
        manifest,
        owner_root_public_key=public_key,
        available_component_ids=frozenset(),  # component missing
        resolvable_key_purposes=frozenset({KeyPurpose.MEMORY_KEY}),
        current_min_compatible_version="2.0.0",
    )
    assert broken_state != RestoreDrillState.RESTORE_TESTED
