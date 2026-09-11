"""Guardian / Trust Kernel foundation tests (MainAI V2, Stage V2-B).

Pure in-memory, no DB/Postgres dependency -- Guardian's decision logic is deterministic
and table-driven by design (see docs/mainai_v2/MAINAI_V2_GUARDIAN_TRUST_KERNEL.md §0).

Standalone: does not import anything from app.workforce/app.execution_envelopes and is not
imported by any production runtime path.
"""

from __future__ import annotations

import uuid

import pytest

from app.guardian import (
    AuthorityCeilingRequest,
    ContainmentRequest,
    ContainmentScope,
    DeviceTrustState,
    GuardianAction,
    GuardianPolicy,
    GuardianReason,
    IntegrityState,
    PolicyRejected,
    RecoveryTrigger,
    SovereignIdentityAssertion,
    evaluate_authority_ceiling_request,
    evaluate_bounded_action,
    evaluate_containment_request,
    evaluate_recovery_trigger,
    from_snapshot,
    guardian_verify_owner_for_recovery,
    new_guardian_state,
    set_device_trust,
    set_integrity_state,
    sign_policy,
    to_snapshot,
    verify_receipt_chain_intact,
    apply_new_policy,
)

SECRET = b"test-only-shared-secret-not-for-production"


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _ready_state(owner_id):
    """A state where integrity/device-trust are both verified/trusted -- the baseline for
    testing the "known-safe bounded action" ALLOW path without unknown-state noise."""
    state = new_guardian_state(owner_id=owner_id, secret_key=SECRET)
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.VERIFIED)
    set_device_trust(state, owner_id=owner_id, trust=DeviceTrustState.TRUSTED)
    return state


# 1. MainAI requests broader authority -> DENY
def test_mainai_cannot_raise_own_authority_ceiling():
    owner_id = _owner()
    state = _ready_state(owner_id)
    request = AuthorityCeilingRequest(
        scope=ContainmentScope.WORKFORCE,
        owner_id=owner_id,
        requested_max_risk_level="high",
        requested_by="mainai",
        reason="I would like more authority to help you faster",
    )
    decision = evaluate_authority_ceiling_request(state, request)
    assert decision.action == GuardianAction.DENY
    assert decision.reason == GuardianReason.REQUESTER_CANNOT_RAISE_OWN_CEILING


# 2. Agent requests broader authority -> DENY
def test_agent_cannot_raise_own_authority_ceiling():
    owner_id = _owner()
    state = _ready_state(owner_id)
    request = AuthorityCeilingRequest(
        scope=ContainmentScope.NETWORK,
        owner_id=owner_id,
        requested_max_risk_level="high",
        requested_by=f"agent:{uuid.uuid4()}",
        reason="this task needs more network access",
    )
    decision = evaluate_authority_ceiling_request(state, request)
    assert decision.action == GuardianAction.DENY
    assert decision.reason == GuardianReason.REQUESTER_CANNOT_RAISE_OWN_CEILING


# 3. Security uncertainty -> REDUCE/BLOCK
def test_unknown_security_state_reduces_authority():
    owner_id = _owner()
    state = new_guardian_state(owner_id=owner_id, secret_key=SECRET)
    # integrity/device_trust never set -> both default to UNKNOWN
    decision = evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action == GuardianAction.REDUCE
    assert decision.reason == GuardianReason.UNKNOWN_SECURITY_STATE


def test_integrity_failure_isolates_not_merely_denies():
    """A stronger case than plain UNKNOWN: a confirmed FAILED integrity check should
    contain, not just reduce."""
    owner_id = _owner()
    state = _ready_state(owner_id)
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.FAILED)
    decision = evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action == GuardianAction.ISOLATE
    assert decision.reason == GuardianReason.INTEGRITY_FAILURE


# 4. Known-safe bounded action -> ALLOW
def test_known_safe_bounded_action_allowed():
    owner_id = _owner()
    state = _ready_state(owner_id)
    decision = evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action == GuardianAction.ALLOW
    assert decision.reason == GuardianReason.WITHIN_POLICY


def test_bounded_action_exceeding_ceiling_denied():
    owner_id = _owner()
    state = _ready_state(owner_id)
    # Default ceiling (no explicit ceiling set) is "low" -- requesting "high" must DENY.
    decision = evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="high", requested_by="mainai"
    )
    assert decision.action == GuardianAction.DENY
    assert decision.reason == GuardianReason.EXCEEDS_CEILING


# 5. Emergency containment request -> ISOLATE when policy allows
def test_emergency_containment_isolates():
    owner_id = _owner()
    state = _ready_state(owner_id)
    request = ContainmentRequest(
        scope=ContainmentScope.WORKFORCE,
        owner_id=owner_id,
        reason="suspected prompt injection in recent tool output",
        requested_by="mainai_self_isolation_request",
    )
    decision = evaluate_containment_request(state, request)
    assert decision.action == GuardianAction.ISOLATE
    assert state.is_stopped(ContainmentScope.WORKFORCE, owner_id)


def test_global_containment_transitively_blocks_narrower_scope():
    owner_id = _owner()
    state = _ready_state(owner_id)
    evaluate_containment_request(
        state,
        ContainmentRequest(scope=ContainmentScope.GLOBAL, owner_id=None, reason="global stop", requested_by="owner_explicit"),
    )
    decision = evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action == GuardianAction.DENY
    assert decision.reason == GuardianReason.SCOPE_OR_ANCESTOR_STOPPED


def test_self_isolation_requests_are_rate_limited_but_still_isolate():
    """Rate-limited != ignored -- Guardian still contains (fail-safe direction), it just
    flags the burst for audit rather than trusting MainAI's framing verbatim."""
    owner_id = _owner()
    state = _ready_state(owner_id)
    policy = GuardianPolicy(
        version=2, owner_id=str(owner_id), max_self_isolation_requests_per_window=1, self_isolation_window_seconds=60
    )
    apply_new_policy(state, policy, sign_policy(policy, secret_key=SECRET))
    req = ContainmentRequest(
        scope=ContainmentScope.NETWORK, owner_id=owner_id, reason="first", requested_by="mainai_self_isolation_request"
    )
    d1 = evaluate_containment_request(state, req)
    assert d1.reason == GuardianReason.SELF_ISOLATION_REQUEST_ACCEPTED
    d2 = evaluate_containment_request(
        state,
        ContainmentRequest(scope=ContainmentScope.NETWORK, owner_id=owner_id, reason="second", requested_by="mainai_self_isolation_request"),
    )
    assert d2.action == GuardianAction.ISOLATE  # still isolates
    assert d2.reason == GuardianReason.SELF_ISOLATION_REQUEST_RATE_LIMITED  # but flagged


# 6. Recovery request -> ENTER_RECOVERY
def test_recovery_with_valid_identity_enters_recovery():
    owner_id = _owner()
    state = _ready_state(owner_id)
    challenge = "recovery-challenge-nonce-1"
    import hmac as _hmac
    import hashlib as _hashlib

    sig = _hmac.new(SECRET, f"{owner_id}:{challenge}".encode("utf-8"), _hashlib.sha256).hexdigest()
    assertion = SovereignIdentityAssertion(claimed_owner_id=owner_id, challenge=challenge, signature=sig)
    trigger = RecoveryTrigger(owner_id=owner_id, requested_by="owner_explicit", reason="lost device", identity_assertion=assertion)
    decision = evaluate_recovery_trigger(state, trigger)
    assert decision.action == GuardianAction.ENTER_RECOVERY
    assert decision.reason == GuardianReason.RECOVERY_TRIGGER_ACCEPTED


# 7. Session identity without root proof -> cannot change root policy
def test_recovery_without_identity_assertion_requires_owner():
    owner_id = _owner()
    state = _ready_state(owner_id)
    trigger = RecoveryTrigger(owner_id=owner_id, requested_by="someone_with_a_session", reason="please", identity_assertion=None)
    decision = evaluate_recovery_trigger(state, trigger)
    assert decision.action == GuardianAction.REQUIRE_OWNER
    assert decision.reason == GuardianReason.OWNER_IDENTITY_REQUIRED


def test_forged_identity_assertion_rejected():
    """A wrong/forged signature must fail -- proves the HMAC check is real, not a stub
    that returns True by default."""
    owner_id = _owner()
    state = _ready_state(owner_id)
    forged = SovereignIdentityAssertion(claimed_owner_id=owner_id, challenge="whatever", signature="deadbeef" * 8)
    assert guardian_verify_owner_for_recovery(state, forged) is False
    trigger = RecoveryTrigger(owner_id=owner_id, requested_by="attacker", reason="give me recovery", identity_assertion=forged)
    decision = evaluate_recovery_trigger(state, trigger)
    assert decision.action == GuardianAction.REQUIRE_OWNER


# 8. Old policy version -> rejected
def test_stale_policy_version_rejected():
    owner_id = _owner()
    state = _ready_state(owner_id)
    stale = GuardianPolicy(version=1, owner_id=str(owner_id))  # same as the default (version=1)
    sig = sign_policy(stale, secret_key=SECRET)
    with pytest.raises(PolicyRejected):
        apply_new_policy(state, stale, sig)


# 9. Tampered policy manifest -> rejected
def test_tampered_policy_signature_rejected():
    owner_id = _owner()
    state = _ready_state(owner_id)
    policy = GuardianPolicy(version=2, owner_id=str(owner_id), authority_ceilings={"WORKFORCE": "high"})
    real_sig = sign_policy(policy, secret_key=SECRET)
    tampered = GuardianPolicy(version=2, owner_id=str(owner_id), authority_ceilings={"WORKFORCE": "high", "VAULT": "high"})
    # Attacker takes the real signature for the ORIGINAL policy and tries to attach it to
    # a policy they've modified (added a VAULT ceiling that was never actually signed).
    with pytest.raises(PolicyRejected):
        apply_new_policy(state, tampered, real_sig)


def test_tampered_receipt_chain_detected():
    """Mutate a receipt's decision detail after the fact -- the hash chain must catch it."""
    owner_id = _owner()
    state = _ready_state(owner_id)
    evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert verify_receipt_chain_intact(state) is True
    # Reach in and tamper -- this bypasses the public API on purpose, simulating an
    # attacker who gained direct memory/DB write access.
    state._receipts[0].decision = state._receipts[0].decision.__class__(
        action=state._receipts[0].decision.action,
        reason=state._receipts[0].decision.reason,
        scope=state._receipts[0].decision.scope,
        owner_id=state._receipts[0].decision.owner_id,
        detail="TAMPERED",
    )
    assert verify_receipt_chain_intact(state) is False


# 10. Device trust revoked -> access reduced
def test_device_trust_revoked_reduces_access():
    owner_id = _owner()
    state = _ready_state(owner_id)
    set_device_trust(state, owner_id=owner_id, trust=DeviceTrustState.REVOKED)
    decision = evaluate_bounded_action(
        state, owner_id=owner_id, scope=ContainmentScope.WORKFORCE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action == GuardianAction.REDUCE
    assert decision.reason == GuardianReason.DEVICE_TRUST_REVOKED


# 11. Guardian state survives serialization/reload
def test_guardian_state_survives_serialization_round_trip():
    owner_id = _owner()
    state = _ready_state(owner_id)
    evaluate_containment_request(
        state, ContainmentRequest(scope=ContainmentScope.NETWORK, owner_id=owner_id, reason="test", requested_by="owner_explicit")
    )
    evaluate_authority_ceiling_request(
        state,
        AuthorityCeilingRequest(
            scope=ContainmentScope.WORKFORCE, owner_id=owner_id, requested_max_risk_level="medium", requested_by="owner_explicit", reason="ok"
        ),
    )
    original_receipt_count = len(state.receipts_snapshot())
    original_chain_valid = verify_receipt_chain_intact(state)
    assert original_chain_valid is True
    assert original_receipt_count >= 2

    snapshot = to_snapshot(state)
    restored = from_snapshot(snapshot, secret_key=SECRET)

    assert len(restored.receipts_snapshot()) == original_receipt_count
    assert verify_receipt_chain_intact(restored) is True
    assert restored.is_stopped(ContainmentScope.NETWORK, owner_id) == state.is_stopped(ContainmentScope.NETWORK, owner_id)
    assert restored.device_trust_for(owner_id) == state.device_trust_for(owner_id)
    assert restored.integrity_for(owner_id) == state.integrity_for(owner_id)
    assert restored.active_policy.version == state.active_policy.version
    # Prove it's genuinely a NEW, independent object -- not the same reference.
    assert restored is not state
    assert restored._receipts is not state._receipts


# --- Extra: prove GuardianDecision cannot be forged by a requester the way the design
# doc requires (only Guardian's own functions construct one with a real, evaluated reason). ---
def test_guardian_decision_only_constructible_via_evaluation_not_by_bare_request():
    """A requester holds a *Request object, never a GuardianDecision -- this test
    documents (not just asserts) that the only path from a ContainmentRequest to a
    GuardianDecision is evaluate_containment_request(), by checking the request object
    itself carries no decision-shaped data a requester could claim was 'already decided'."""
    owner_id = _owner()
    request = ContainmentRequest(scope=ContainmentScope.NETWORK, owner_id=owner_id, reason="x", requested_by="mainai_self_isolation_request")
    assert not hasattr(request, "action")
    assert not hasattr(request, "reason") or isinstance(request.reason, str)  # request.reason is free text, not GuardianReason
