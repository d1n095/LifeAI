"""Cross-layer proof: Sentinel (app.sentinel), Guardian (app.guardian), and the Privacy
Boundary Engine (app.privacy_boundary) are three deliberately independent, non-coupled
packages -- none of the three imports either of the other two (see each package's own
__init__.py docstring). That independence is exactly what this test file exists to stress:
a real future composed caller must get the correct outcome from combining them, and no
single layer's permissiveness may be sufficient on its own.

SENTINEL DETECTION != AUTHORITY: Sentinel can only ever produce a DefensiveActionRequest (a
request). Turning that into an actual contained state requires a *composed caller* -- code
that does not exist yet anywhere in the production runtime -- to translate the request into
a Guardian ContainmentRequest and get Guardian's own independent decision. This test file
is that composed caller's proof-of-concept, in test form only.

None of the three packages are imported by any production runtime path. This test file is
the only place in the whole MainAI V2 lane where all three are used together, and it is
itself not imported by anything else.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

from app.guardian import (
    ContainmentRequest,
    ContainmentScope,
    DeviceTrustState,
    GuardianAction,
    IntegrityState,
    evaluate_bounded_action,
    evaluate_containment_request,
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
    new_sentinel_state,
    record_event,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _event(
    *,
    owner_id: uuid.UUID,
    device_id: str = "device-1",
    event_type: SecurityEventType,
    details: dict | None = None,
    occurred_at: datetime | None = None,
) -> SecurityEvent:
    return SecurityEvent(
        event_id=uuid.uuid4(),
        event_type=event_type,
        severity=SecuritySeverity.MEDIUM,
        confidence=SecurityConfidence.MEDIUM,
        subject=SecuritySubject(owner_id=owner_id, device_id=device_id, subject_kind="process", subject_ref="subject-ref"),
        source=SecuritySource(adapter_name="test_adapter", adapter_version="0.1.0"),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        correlation_id=uuid.uuid4(),
        parent_event_id=None,
        details=details or {},
    )


def _healthy_guardian_state(owner_id: uuid.UUID):
    """Mirrors test_cross_layer_guardian_privacy.py's _healthy_state(): a fresh GuardianState
    defaults integrity/device-trust to UNKNOWN (fail-closed) until explicitly established."""
    state = new_guardian_state(owner_id=owner_id, secret_key=b"test-secret-key-32-bytes-min!!")
    set_integrity_state(state, owner_id=owner_id, integrity=IntegrityState.VERIFIED)
    set_device_trust(state, owner_id=owner_id, trust=DeviceTrustState.TRUSTED)
    return state


# --- Scenario 1 (founder's own example): critical exfiltration -> Sentinel requests
# containment -> Guardian evaluates -> bounded action decision produced. -------------------


def test_sentinel_exfiltration_incident_requests_guardian_containment():
    owner_id = _owner()
    sentinel_state = new_sentinel_state()
    now = datetime.now(timezone.utc)

    # Drive the full 4-indicator exfiltration pattern to CONFIRMED (same shape as
    # test_sentinel_foundation.py's own exfiltration test).
    record_event(sentinel_state, _event(owner_id=owner_id, event_type=SecurityEventType.USB_CONNECTED, occurred_at=now))
    record_event(
        sentinel_state,
        _event(owner_id=owner_id, event_type=SecurityEventType.PROCESS_STARTED, details={"process_known": False}, occurred_at=now + timedelta(seconds=1)),
    )
    record_event(sentinel_state, _event(owner_id=owner_id, event_type=SecurityEventType.MASS_FILE_READ, occurred_at=now + timedelta(seconds=2)))
    _, _, touched = record_event(sentinel_state, _event(owner_id=owner_id, event_type=SecurityEventType.UNEXPECTED_EGRESS, occurred_at=now + timedelta(seconds=3)))

    assert len(touched) == 1
    incident = touched[0]
    assert incident.threat_class == ThreatClass.EXFILTRATION
    assert incident.state == IncidentState.CONFIRMED

    # Sentinel's own output: a pure-data request. No preauthorization attached -> this is
    # the "owner-response-required" path, not an auto-executing one.
    defensive_request = build_defensive_action_request(
        incident,
        action=DefensiveAction.SWITCH_LOCAL_ONLY,
        scope_hint="network:outbound",
        reason="confirmed exfiltration pattern: USB + unknown process + mass file read + unexpected egress",
    )
    assert defensive_request.incident_id == incident.incident_id
    assert defensive_request.preauthorized_by is None

    # DefensiveActionRequest is pure data -- structurally cannot execute anything itself.
    assert not hasattr(defensive_request, "execute")
    assert not hasattr(defensive_request, "apply")

    # The composed caller (this test, standing in for future integration code) translates
    # the request into a Guardian ContainmentRequest. Sentinel itself never did this --
    # app.sentinel has no import of app.guardian anywhere (proven structurally in
    # test_sentinel_foundation.py's own suite; re-asserted here for the composed path too).
    import re

    import app.sentinel as sentinel_pkg

    sentinel_source = inspect.getsource(sentinel_pkg)
    import_line = re.compile(r"^\s*(import|from)\s+app\.guardian\b", re.MULTILINE)
    assert not import_line.search(sentinel_source), "app.sentinel must not import app.guardian anywhere"

    guardian_state = _healthy_guardian_state(owner_id)
    containment_request = ContainmentRequest(
        scope=ContainmentScope.SENTINEL_RESPONSE,
        owner_id=owner_id,
        reason=defensive_request.reason,
        requested_by=f"sentinel:{defensive_request.action.value}",
    )
    decision = evaluate_containment_request(guardian_state, containment_request)

    # Guardian's containment path is unconditional (fail-safe direction) once a request
    # reaches it -- but reaching it required a real composed caller to translate Sentinel's
    # request into Guardian's own vocabulary. Sentinel's CONFIRMED incident alone was never
    # sufficient by itself; it produced a request, not a decision.
    assert decision.action == GuardianAction.ISOLATE
    assert decision.scope == ContainmentScope.SENTINEL_RESPONSE

    # And the isolation is now visible independently through Guardian's own bounded-action
    # check for that scope -- proving this was a real state change, not just a returned value.
    follow_up = evaluate_bounded_action(
        guardian_state, owner_id=owner_id, scope=ContainmentScope.SENTINEL_RESPONSE, requested_risk_level="low", requested_by="mainai"
    )
    assert follow_up.action != GuardianAction.ALLOW


# --- Scenario 2 (founder's own example): security event includes raw personal content ->
# Privacy layer blocks/minimizes external learning signal -> local incident state still
# retains only the safe evidence needed. ---------------------------------------------------


def test_privacy_boundary_blocks_raw_content_a_sentinel_adapter_might_have_seen():
    owner_id = _owner()
    sentinel_state = new_sentinel_state()

    # Sentinel's OWN defense: a real adapter cannot get raw personal content into a
    # SecurityEvent's `details` in the first place (app.sentinel.service._validate_details).
    from app.sentinel import SentinelPrivacyViolation

    tainted_event = _event(
        owner_id=owner_id,
        event_type=SecurityEventType.MASS_FILE_READ,
        details={"raw_content": "the owner's actual private document text"},
    )
    caught = None
    try:
        record_event(sentinel_state, tainted_event)
    except SentinelPrivacyViolation as exc:
        caught = exc
    assert caught is not None, "Sentinel's own detail validator must reject a raw_content-shaped field"
    assert sentinel_state.incidents_snapshot() == (), "a rejected event must leave no trace in incident state"

    # Independent, second layer: even if a hypothetical future adapter captured the SAME raw
    # observation upstream of Sentinel's classification step (e.g. before it was ever turned
    # into a SecurityEvent) and a composed caller tried to ship it as an outbound LEARNING
    # signal, Privacy Boundary blocks it entirely on its own -- it has no knowledge of
    # Sentinel or the validator above, and does not need any to reach the correct outcome.
    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="file_scanner_observation",
        raw_content={"path": "/Users/owner/private/medical_records.pdf", "excerpt": "the owner's actual private document text"},
        classification=DataClassification.PRIVATE,
    )
    receipt_log = ReceiptLog.default()
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.LEARNING,
        purpose=OutboundPurpose.LEARNING_SIGNAL,
        module="test_cross_layer_sentinel",
        software_version="0.0.0-test",
        skill="security_event_classification",
        failure_class="knowledge_gap",
        success=False,
        receipt_log=receipt_log,
    )
    assert result is None or "the owner's actual private document text" not in repr(result)
    for receipt in receipt_log.all():
        assert "the owner's actual private document text" not in repr(receipt)

    # Meanwhile, a REAL incident built only from validated, already-classified events keeps
    # working normally -- the two-layer defense above does not degrade Sentinel's own
    # legitimate function. Its evidence notes are short synthesized strings, never raw text.
    _, _, touched = record_event(
        sentinel_state,
        _event(owner_id=owner_id, event_type=SecurityEventType.CREDENTIAL_READ_ATTEMPT),
    )
    _, _, touched2 = record_event(
        sentinel_state,
        _event(owner_id=owner_id, event_type=SecurityEventType.NEW_OUTBOUND_DESTINATION),
    )
    assert len(touched2) == 1
    incident = touched2[0]
    for evidence in incident.evidence:
        assert len(evidence.note) < 200
        assert "medical_records" not in evidence.note
        assert "the owner's actual private document text" not in evidence.note


# --- Scenario 3: unknown security state + an outbound signal derived from Sentinel activity
# -> authority REDUCED (Guardian) and egress BLOCKED (Privacy Boundary), independently. -----


def test_unknown_security_state_reduces_guardian_authority_and_blocks_sentinel_derived_egress():
    owner_id = _owner()
    guardian_state = new_guardian_state(owner_id=owner_id, secret_key=b"test-secret-key-32-bytes-min!!")

    # Something (e.g. a not-yet-classified Sentinel signal) made integrity state unknown.
    set_integrity_state(guardian_state, owner_id=owner_id, integrity=IntegrityState.UNKNOWN)

    decision = evaluate_bounded_action(
        guardian_state, owner_id=owner_id, scope=ContainmentScope.SENTINEL_RESPONSE, requested_risk_level="low", requested_by="mainai"
    )
    assert decision.action == GuardianAction.REDUCE, f"UNKNOWN SECURITY STATE -> REDUCE/BLOCK is required, got {decision.action}"

    # Independently: a hypothetical outbound "incident summary" learning signal, carrying a
    # PRIVATE-classified detail an owner would not want centrally aggregated, is evaluated by
    # Privacy Boundary fully on its own merits -- it has no concept of Guardian's integrity
    # state at all, by design. Both layers reach a blocking/reducing outcome independently,
    # neither one inheriting permission or denial from the other.
    raw_signal = RawLocalSignal(
        owner_id=owner_id,
        domain="sentinel_incident_summary",
        raw_content={"note": "owner's device saw 4 correlated signals from an unrecognized USB device today"},
        classification=DataClassification.PRIVATE,
    )
    result = run_privacy_pipeline(
        raw_signal,
        mode=TelemetryMode.LEARNING,
        purpose=OutboundPurpose.LEARNING_SIGNAL,
        module="test_cross_layer_sentinel",
        software_version="0.0.0-test",
        skill="incident_summary_export",
        failure_class="knowledge_gap",
        success=False,
    )

    # Composed-caller rule: Guardian's REDUCE alone is sufficient to block the composed
    # decision, independent of whatever Privacy Boundary decided on its own merits.
    assert decision.action != GuardianAction.ALLOW
    # And this scenario's raw PRIVATE content must never surface unminimized in any result.
    assert result is None or "unrecognized USB device" not in repr(result)
