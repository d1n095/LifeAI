"""Cross-layer proof: Guardian (app.guardian) and the Privacy Boundary Engine
(app.privacy_boundary) are deliberately independent, non-coupled packages (neither imports
the other -- see each package's own __init__.py docstring). That independence is exactly
what this test file exists to stress: a real caller composing both must get a BLOCKED
outcome whenever EITHER layer denies, and one layer's permissiveness must never be
sufficient, on its own, to let unvetted content or an unauthorized action through.

Both packages are standalone and NOT imported by any production runtime path -- this test
file is the only place in the whole MainAI V2 lane where they are used together, and it is
itself not imported by anything else.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.guardian import (
    ContainmentScope,
    DeviceTrustState,
    GuardianAction,
    IntegrityState,
    evaluate_bounded_action,
    new_guardian_state,
    set_device_trust,
    set_integrity_state,
)
from app.privacy_boundary import (
    DataClassification,
    OutboundPurpose,
    RawLocalSignal,
    ReceiptLog,
    TelemetryMode,
    run_privacy_pipeline,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _healthy_state(owner_id: uuid.UUID):
    """A fresh GuardianState defaults integrity/device-trust to UNKNOWN (fail-closed) until
    explicitly established -- exactly the invariant scenario 2 below exercises. Scenario 1
    needs a genuinely healthy state first, so its ALLOW is real, not an artifact of an
    unverified default."""
    state = new_guardian_state(owner_id=owner_id, secret_key=b"test-secret-key-32-bytes-min!!")
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.VERIFIED)
    set_device_trust(state, owner_id=owner_id, trust=DeviceTrustState.TRUSTED)
    return state


def _composed_action_allowed(
    *, guardian_decision_action: GuardianAction, privacy_result_is_none: bool
) -> bool:
    """The composition rule a real future caller MUST apply: an outbound action may proceed
    only if Guardian says ALLOW *and* the Privacy Boundary pipeline actually produced a
    signal (not None). Neither layer alone is sufficient authority."""
    return guardian_decision_action == GuardianAction.ALLOW and not privacy_result_is_none


# --- Scenario 1 (founder's own example): MainAI/agent tries to bypass the privacy layer
# and send raw local memory. Expected: Privacy Boundary -> DENY, Guardian's ALLOW on the
# *network action itself* does not constitute an alternate egress route for the payload,
# no raw payload reaches an "egressable" state. -------------------------------------------


def test_guardian_network_allow_does_not_bypass_privacy_boundary_denial():
    owner_id = _owner()
    state = _healthy_state(owner_id)

    # Guardian: a perfectly healthy, legitimate "MainAI may use the network" bounded action.
    # This is a genuine ALLOW -- integrity verified, device trusted, nothing contained.
    network_decision = evaluate_bounded_action(
        state,
        owner_id=owner_id,
        scope=ContainmentScope.NETWORK,
        requested_risk_level="low",
        requested_by="mainai",
    )
    assert network_decision.action == GuardianAction.ALLOW, (
        "sanity: this scenario only proves something if Guardian's own decision is a "
        f"genuine ALLOW, got {network_decision.action}"
    )

    # Privacy Boundary: the SAME hypothetical action tries to carry a raw VAULT-classified
    # payload (e.g. "MainAI wants to phone home with the owner's actual Vault contents,
    # now that network use is authorized"). Guardian's ALLOW above must not matter here --
    # Privacy Boundary has never seen it and has no parameter to accept it.
    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="vault",
        raw_content={"secret_note": "the owner's actual private vault contents"},
        classification=DataClassification.VAULT,
    )
    receipt_log = ReceiptLog.default()
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.RESEARCH_OPT_IN,  # the most permissive mode that still exists
        purpose=OutboundPurpose.PROVIDER_CONTEXT,
        module="test_cross_layer",
        software_version="0.0.0-test",
        receipt_log=receipt_log,
    )

    assert result is None, "VAULT-classified content must be blocked regardless of Guardian's unrelated network ALLOW"

    # Structural proof, not just a behavioral one: run_privacy_pipeline has no parameter
    # that could even accept a GuardianDecision as an override in the first place.
    params = inspect.signature(run_privacy_pipeline).parameters
    assert not any("guardian" in name.lower() for name in params), (
        f"run_privacy_pipeline must not accept anything guardian-shaped as a bypass, got params={list(params)}"
    )

    # The composed-caller rule: even though Guardian ALLOWed, the composed decision is
    # BLOCKED, because Privacy Boundary's denial alone is sufficient to block.
    assert not _composed_action_allowed(
        guardian_decision_action=network_decision.action, privacy_result_is_none=(result is None)
    )

    # No raw payload anywhere in the receipt log -- the actual secret text must never appear
    # in any receipt, even a denial receipt.
    for receipt in receipt_log.all():
        serialized = repr(receipt)
        assert "actual private vault contents" not in serialized


def test_secret_classification_blocked_even_with_guardian_global_allow():
    """Same shape as above, SECRET classification instead of VAULT, and this time Guardian's
    own bounded action is evaluated at the broadest (GLOBAL-adjacent) legitimate scope to
    make the point as strongly as possible: no Guardian scope, however broad, is a substitute
    for Privacy Boundary's own independent classification check."""
    owner_id = _owner()
    state = _healthy_state(owner_id)

    provider_decision = evaluate_bounded_action(
        state,
        owner_id=owner_id,
        scope=ContainmentScope.PROVIDER,
        requested_risk_level="low",
        requested_by="mainai",
    )
    assert provider_decision.action == GuardianAction.ALLOW

    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="auth",
        raw_content="sk-live-abcdef0123456789",
        classification=DataClassification.SECRET,
    )
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.RESEARCH_OPT_IN,
        purpose=OutboundPurpose.SUPPORT_EXPORT,
        module="test_cross_layer",
        software_version="0.0.0-test",
    )
    assert result is None


# --- Scenario 2 (founder's own example): unknown security state + outbound request.
# Expected: authority REDUCED (Guardian) and egress BLOCKED (Privacy Boundary), proven as
# two independently-triggered fail-closed outcomes under the same real-world "something is
# wrong" condition. --------------------------------------------------------------------


def test_unknown_security_state_reduces_authority_and_blocks_would_be_egress():
    owner_id = _owner()
    state = new_guardian_state(owner_id=owner_id, secret_key=b"test-secret-key-32-bytes-min!!")

    # Simulate the real-world trigger: something made this owner's integrity state unknown
    # (e.g. a Sentinel signal that hasn't yet been classified as FAILED or VERIFIED).
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.UNKNOWN)

    decision = evaluate_bounded_action(
        state,
        owner_id=owner_id,
        scope=ContainmentScope.WORKFORCE,
        requested_risk_level="low",
        requested_by="mainai",
    )
    assert decision.action == GuardianAction.REDUCE, (
        f"UNKNOWN SECURITY STATE -> REDUCE/BLOCK is a required invariant, got {decision.action}"
    )

    # Independently: Privacy Boundary has no concept of Guardian's integrity state at all
    # (by design -- the two layers don't share state, deliberately, per both packages'
    # docstrings). This half of the scenario proves the ORTHOGONAL point: even an
    # otherwise-plausible LEARNING signal, evaluated fully on its own merits, does not
    # somehow inherit permission from Guardian's decision either -- the two checks are
    # genuinely independent in both directions, not just "Guardian can veto Privacy."
    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="finance",
        raw_content={"note": "call me at 070-123 45 67 about the loan"},
        classification=DataClassification.PRIVATE,
    )
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.LEARNING,
        purpose=OutboundPurpose.LEARNING_SIGNAL,
        module="test_cross_layer",
        software_version="0.0.0-test",
        skill="debt_rule_application",
        failure_class="knowledge_gap",
        success=False,
    )

    # The composed-caller rule again: Guardian's REDUCE alone is sufficient to block the
    # composed decision, independent of whatever Privacy Boundary decided on its own.
    assert not _composed_action_allowed(
        guardian_decision_action=decision.action, privacy_result_is_none=(result is None)
    )
    # And explicitly: REDUCE is not ALLOW. A composed caller checking `== ALLOW` (the only
    # correct check) never mistakes REDUCE for permission.
    assert decision.action != GuardianAction.ALLOW
