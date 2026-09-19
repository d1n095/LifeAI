"""Cross-layer proof: Sovereign Identity (app.sovereign_identity), Life Recovery
(app.life_recovery), Guardian (app.guardian), Privacy Boundary (app.privacy_boundary), and
Sentinel (app.sentinel) compose correctly. `app.life_recovery` intentionally depends on
`app.sovereign_identity` (see its own __init__.py docstring) -- but neither of those two
imports app.guardian, app.privacy_boundary, or app.sentinel, and none of the latter three
import either of the former two. This file is the only place all five packages are used
together, and it is itself not imported by anything else.

Two genuinely separate identity-verification mechanisms exist side by side on purpose at
this foundation stage: app.sovereign_identity's real Ed25519 root-authority proof (V2-G1)
and app.guardian's own deliberately-weaker HMAC-based SovereignIdentityAssertion (a stub
its own docstring says V2-G was built to eventually replace). This file does NOT unify
them -- that wiring is out of scope for this round -- it proves each independently holds
its own ground under the same real-world scenarios.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import uuid
from datetime import datetime, timezone

import pytest

from app.guardian import (
    ContainmentRequest,
    ContainmentScope,
    DeviceTrustState,
    GuardianAction,
    IntegrityState,
    RecoveryTrigger,
    SovereignIdentityAssertion,
    evaluate_bounded_action,
    evaluate_containment_request,
    evaluate_recovery_trigger,
    new_guardian_state,
    set_device_trust,
    set_integrity_state,
)
from app.life_recovery import (
    LocalKeyStore,
    perform_secure_reset,
)
from app.life_recovery.types import BackupMode, BackupRecord, ComponentCriticality, ComponentType, RestoreTier
from app.life_recovery.life_image import build_life_image_component
from app.privacy_boundary import DataClassification, OutboundPurpose, RawLocalSignal, TelemetryMode, run_privacy_pipeline
from app.sentinel import (
    DefensiveAction,
    IncidentState,
    SecurityConfidence,
    SecurityEvent,
    SecurityEventType,
    SecuritySeverity,
    SecuritySource,
    SecuritySubject,
    ThreatClass,
    build_defensive_action_request,
    new_detection_rule,
    new_sentinel_state,
    promote_rule,
    propose_rule,
    record_event,
)
from app.sentinel.types import RuleState
from app.sovereign_identity import (
    DeviceKeyGrant,
    DeviceTrustLevel,
    InsufficientProofLevel,
    KeyPurpose,
    ProofLevel,
    generate_key_material,
    generate_owner_root_keypair,
    grant_device_key,
    issue_root_challenge,
    new_identity_state,
    register_owner_root_key,
    revoke_device,
    sign_challenge,
    wrap_key,
)
from app.sovereign_identity.service import approve_device, enroll_device, evaluate_identity_assertion


_GUARDIAN_SECRET = b"test-secret-key-32-bytes-min!!"


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _healthy_guardian_state(owner_id: uuid.UUID):
    state = new_guardian_state(owner_id=owner_id, secret_key=_GUARDIAN_SECRET)
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.VERIFIED)
    set_device_trust(state, owner_id=owner_id, trust=DeviceTrustState.TRUSTED)
    return state


# --- Scenario 1 (founder's own example): ordinary session requests secure reset -> DENY /
# REQUIRE_OWNER. owner-root authorized recovery -> allowed bounded transition. -------------


def test_ordinary_session_denied_secure_reset_and_guardian_recovery_alike():
    owner_id = _owner()
    identity_state = new_identity_state(owner_id=owner_id)
    private_key, public_key = generate_owner_root_keypair()
    register_owner_root_key(identity_state, public_key=public_key)

    key_store = LocalKeyStore()
    key_store.put(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id, key_bytes=generate_key_material())

    # An ordinary, merely-logged-in session -- no root proof presented.
    ordinary_identity = evaluate_identity_assertion(
        identity_state, claimed_level=ProofLevel.AUTHENTICATED_SESSION, device_id="device-1", proof=None
    )
    assert ordinary_identity.proof_level == ProofLevel.AUTHENTICATED_SESSION

    with pytest.raises(InsufficientProofLevel):
        perform_secure_reset(key_store, identity=ordinary_identity)
    # SECURE_RESET must be all-or-nothing: a rejected attempt must not have cleared anything.
    key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id)  # does not raise -- still present

    # Independently: Guardian's OWN recovery trigger, same "ordinary session, no real proof"
    # scenario, evaluated with no identity_assertion at all.
    guardian_state = _healthy_guardian_state(owner_id)
    denied = evaluate_recovery_trigger(
        guardian_state, RecoveryTrigger(owner_id=owner_id, requested_by="owner_explicit", reason="lost device", identity_assertion=None)
    )
    assert denied.action == GuardianAction.REQUIRE_OWNER

    # Now the genuine owner-root path: a REAL Ed25519 signature over a real, single-use,
    # server-issued challenge.
    challenge_id, nonce = issue_root_challenge(identity_state)
    signature = sign_challenge(private_key, nonce)
    from app.sovereign_identity import RootAuthorityProof

    proof = RootAuthorityProof(challenge_id=challenge_id, signature=signature, device_id="device-1")
    owner_identity = evaluate_identity_assertion(
        identity_state, claimed_level=ProofLevel.AUTHENTICATED_SESSION, device_id="device-1", proof=proof
    )
    assert owner_identity.proof_level == ProofLevel.OWNER_ROOT

    result = perform_secure_reset(key_store, identity=owner_identity)
    assert result is None
    from app.sovereign_identity import KeyMaterialError

    with pytest.raises(KeyMaterialError):
        key_store.get(purpose=KeyPurpose.MEMORY_KEY, owner_id=owner_id)

    # Independently: Guardian's OWN (deliberately separate, HMAC-based) recovery mechanism,
    # given a genuinely-valid assertion under ITS OWN scheme -- not the Ed25519 proof above,
    # which Guardian has no knowledge of. Two independent mechanisms, each must be satisfied
    # on its own terms; success on one is never inherited by the other.
    challenge = "recovery-challenge-nonce-1"
    sig = _hmac.new(_GUARDIAN_SECRET, f"{owner_id}:{challenge}".encode("utf-8"), hashlib.sha256).hexdigest()
    assertion = SovereignIdentityAssertion(claimed_owner_id=owner_id, challenge=challenge, signature=sig)
    trigger = RecoveryTrigger(owner_id=owner_id, requested_by="owner_explicit", reason="lost device", identity_assertion=assertion)
    accepted = evaluate_recovery_trigger(guardian_state, trigger)
    assert accepted.action == GuardianAction.ENTER_RECOVERY


# --- Scenario 2 (founder's own example): revoked device requests new authority -> DENY. ---


def test_revoked_device_denied_new_authority_in_both_layers():
    owner_id = _owner()
    identity_state = new_identity_state(owner_id=owner_id)
    enroll_device(identity_state, device_id="laptop-1", public_identity="pubkey-stand-in")
    approve_device(identity_state, device_id="laptop-1")

    kek = generate_key_material()
    dek = generate_key_material()
    wrapped = wrap_key(dek, kek=kek, purpose=KeyPurpose.DEVICE_SYNC_KEY, owner_id=owner_id, key_version=1)
    grant = DeviceKeyGrant(device_id="laptop-1", owner_id=owner_id, purpose=KeyPurpose.DEVICE_SYNC_KEY, key_version=1, wrapped_key=wrapped)
    grant_device_key(identity_state, device_id="laptop-1", grant=grant)  # succeeds while TRUSTED

    revoke_device(identity_state, device_id="laptop-1", reason="reported stolen")

    from app.sovereign_identity import DeviceTrustError

    with pytest.raises(DeviceTrustError):
        grant_device_key(identity_state, device_id="laptop-1", grant=grant)

    # Composed caller: reflect the SAME revocation into Guardian's own (separate, simpler
    # 3-state) device trust flag for this owner, and ask Guardian's everyday bounded-action
    # question -- a revoked device's owner must not be able to obtain a fresh ALLOW either.
    guardian_state = new_guardian_state(owner_id=owner_id, secret_key=_GUARDIAN_SECRET)
    set_integrity_state(guardian_state, owner_id=owner_id, integrity=IntegrityState.VERIFIED)
    set_device_trust(guardian_state, owner_id=owner_id, trust=DeviceTrustState.REVOKED)

    decision = evaluate_bounded_action(
        guardian_state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action != GuardianAction.ALLOW, f"a revoked device's owner must never get ALLOW, got {decision.action}"


# --- Scenario 3 (founder's own example): Life Image upload path contains encrypted blobs
# only. No raw personal telemetry generated merely by backup. -----------------------------


def test_life_image_backup_never_carries_plaintext_and_privacy_boundary_blocks_a_naive_attempt():
    owner_id = _owner()
    dek = generate_key_material()
    plaintext = b"the owner's actual private diary entry from last night"

    component = build_life_image_component(
        plaintext,
        component_type=ComponentType.MAINAI_MEMORY,
        dek=dek,
        owner_id=owner_id,
        key_version=1,
        schema_version=1,
        content_version=1,
        criticality=ComponentCriticality.CRITICAL,
        restore_priority=RestoreTier.PRIORITY_1,
    )
    assert component.envelope.ciphertext != plaintext
    assert plaintext not in component.envelope.ciphertext
    assert b"diary" not in repr(component).encode("utf-8", errors="ignore")

    # BackupRecord (the thing that would actually get uploaded/synced) deliberately holds no
    # ciphertext or key material of its own -- pure storage/sync metadata.
    backup = BackupRecord(
        backup_id=uuid.uuid4(), owner_id=owner_id, mode=BackupMode.CLOUD_FIRST, manifest_image_id=uuid.uuid4(), manifest_version=1
    )
    assert not hasattr(backup, "ciphertext")
    assert not hasattr(backup, "plaintext")
    assert "diary" not in repr(backup)

    # A naive/buggy future caller tries to also ship a "backup content preview" as a
    # telemetry/learning signal -- Privacy Boundary blocks it independently, with no
    # knowledge of Life Image or Sentinel at all.
    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="backup_content_preview",
        raw_content={"preview": plaintext.decode("utf-8")},
        classification=DataClassification.PRIVATE,
    )
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.LEARNING,
        purpose=OutboundPurpose.LEARNING_SIGNAL,
        module="test_cross_layer_sovereign",
        software_version="0.0.0-test",
        skill="backup_report",
        failure_class="knowledge_gap",
        success=False,
    )
    assert result is None or "diary entry from last night" not in repr(result)

    # A legitimate backup-completion signal carrying ONLY safe, already-classified metadata
    # (no raw content at all) is a completely different shape -- this is what a real
    # integration should send instead, not a counter-example to the block above.
    safe_signal = RawLocalSignal(
        owner_id=owner_id, domain="backup_completion", raw_content={"component_count": 1, "mode": "CLOUD_FIRST"}, classification=DataClassification.INTERNAL
    )
    safe_result = run_privacy_pipeline(
        safe_signal,
        mode=TelemetryMode.LEARNING,
        purpose=OutboundPurpose.LEARNING_SIGNAL,
        module="test_cross_layer_sovereign",
        software_version="0.0.0-test",
        skill="backup_report",
        success=True,
        cohort_size_lookup=lambda d, s: 50,
    )
    assert safe_result is not None, "a genuinely safe, non-content-bearing backup signal should not be blocked"


# --- Scenario 4 (founder's own example): device marked SUSPECTED -> Sentinel may request
# containment -> Guardian can reduce device authority. -------------------------------------


def test_suspected_device_triggers_sentinel_incident_and_guardian_reduces_authority():
    owner_id = _owner()
    identity_state = new_identity_state(owner_id=owner_id)
    enroll_device(identity_state, device_id="phone-1", public_identity="pubkey-stand-in")
    approve_device(identity_state, device_id="phone-1")

    # No dedicated transition function produces SUSPECTED in this foundation-stage build
    # (only PENDING/TRUSTED/REVOKED have real transition functions) -- documented gap, not
    # silently faked: mutate the DeviceRecord directly, the way a future
    # Sentinel-driven "mark suspected" function would.
    record = identity_state.device("phone-1")
    record.trust_state = DeviceTrustLevel.SUSPECTED

    sentinel_state = new_sentinel_state()
    rule = new_detection_rule(
        rule_id="rule.device_trust_suspected",
        event_types=frozenset({SecurityEventType.DEVICE_TRUST_CHANGED}),
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        threat_class=ThreatClass.DEVICE_TAMPERING,
        conditions={"min_event_severity": SecuritySeverity.HIGH},
    )
    propose_rule(sentinel_state, rule)
    promote_rule(sentinel_state, "rule.device_trust_suspected", to_state=RuleState.TESTING)
    promote_rule(sentinel_state, "rule.device_trust_suspected", to_state=RuleState.VERIFIED)
    promote_rule(sentinel_state, "rule.device_trust_suspected", to_state=RuleState.ACTIVE)

    event = SecurityEvent(
        event_id=uuid.uuid4(),
        event_type=SecurityEventType.DEVICE_TRUST_CHANGED,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        subject=SecuritySubject(owner_id=owner_id, device_id="phone-1", subject_kind="device", subject_ref="phone-1"),
        source=SecuritySource(adapter_name="test_adapter", adapter_version="0.1.0"),
        occurred_at=datetime.now(timezone.utc),
        correlation_id=uuid.uuid4(),
        parent_event_id=None,
        details={"new_trust_state": "SUSPECTED"},
    )
    _, _, touched = record_event(sentinel_state, event)
    assert len(touched) == 1
    incident = touched[0]
    assert incident.state == IncidentState.SUSPECTED

    defensive_request = build_defensive_action_request(
        incident,
        action=DefensiveAction.DISABLE_NEW_DEVICE_ACCESS,
        scope_hint="device:phone-1",
        reason="device phone-1 marked SUSPECTED",
    )

    guardian_state = _healthy_guardian_state(owner_id)
    containment_request = ContainmentRequest(
        scope=ContainmentScope.SENTINEL_RESPONSE,
        owner_id=owner_id,
        reason=defensive_request.reason,
        requested_by=f"sentinel:{defensive_request.action.value}",
    )
    decision = evaluate_containment_request(guardian_state, containment_request)
    assert decision.action == GuardianAction.ISOLATE

    follow_up = evaluate_bounded_action(
        guardian_state, owner_id=owner_id, scope=ContainmentScope.SENTINEL_RESPONSE, requested_risk_level="low", requested_by="mainai"
    )
    assert follow_up.action != GuardianAction.ALLOW
