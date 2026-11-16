"""Sentinel foundation tests (MainAI V2, Stages V2-D1..D4).

Pure in-memory, no DB/Postgres dependency -- Sentinel's correlation logic is deterministic
and table-driven by design (see docs/mainai_v2/MAINAI_V2_SENTINEL_SECURITY.md).

Standalone: does not import app.guardian or app.privacy_boundary, and is not imported by
any production runtime path.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.sentinel import (
    DefensiveAction,
    IncidentState,
    PreauthorizedDefense,
    RuleState,
    SecurityConfidence,
    SecurityEvent,
    SecurityEventType,
    SecuritySeverity,
    SecuritySource,
    SecuritySubject,
    SentinelPrivacyViolation,
    SentinelRuleError,
    ThreatClass,
    build_canary_touch_event,
    build_defensive_action_request,
    evaluate_event_against_registry,
    from_snapshot,
    mark_false_positive,
    new_detection_rule,
    new_sentinel_state,
    promote_rule,
    propose_rule,
    publish_new_version,
    record_event,
    register_canary,
    require_bounded_preauthorization,
    revoke_rule,
    rollback_rule,
    rule_matches_event,
    to_snapshot,
    verify_event_chain_intact,
)


def _owner() -> uuid.UUID:
    return uuid.uuid4()


def _event(
    *,
    owner_id: uuid.UUID,
    device_id: str = "device-1",
    event_type: SecurityEventType,
    severity: SecuritySeverity = SecuritySeverity.MEDIUM,
    confidence: SecurityConfidence = SecurityConfidence.MEDIUM,
    subject_ref: str = "subject-ref",
    subject_kind: str = "process",
    details: dict | None = None,
    occurred_at: datetime | None = None,
) -> SecurityEvent:
    return SecurityEvent(
        event_id=uuid.uuid4(),
        event_type=event_type,
        severity=severity,
        confidence=confidence,
        subject=SecuritySubject(owner_id=owner_id, device_id=device_id, subject_kind=subject_kind, subject_ref=subject_ref),
        source=SecuritySource(adapter_name="test_adapter", adapter_version="0.1.0"),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        correlation_id=uuid.uuid4(),
        parent_event_id=None,
        details=details or {},
    )


def _active_rule(state, *, rule_id: str, event_types, min_severity: SecuritySeverity, threat_class: ThreatClass, severity: SecuritySeverity, confidence: SecurityConfidence, conditions: dict | None = None):
    rule = new_detection_rule(
        rule_id=rule_id,
        event_types=event_types,
        severity=severity,
        confidence=confidence,
        threat_class=threat_class,
        conditions=conditions or {"min_event_severity": min_severity},
    )
    propose_rule(state, rule)
    promote_rule(state, rule_id, to_state=RuleState.TESTING)
    promote_rule(state, rule_id, to_state=RuleState.VERIFIED)
    return promote_rule(state, rule_id, to_state=RuleState.ACTIVE)


# 1. single benign event -> no incident
def test_single_benign_event_opens_no_incident():
    state = new_sentinel_state()
    owner = _owner()
    event = _event(owner_id=owner, event_type=SecurityEventType.PROCESS_STARTED, severity=SecuritySeverity.LOW, details={"process_known": True})
    _, receipt, touched = record_event(state, event)
    assert touched == []
    assert receipt.deduplicated is False
    assert state.incidents_snapshot() == ()


# 2. single suspicious event -> SUSPECTED incident
def test_single_suspicious_event_opens_suspected_incident():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.unsigned_binary",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH)
    _, _, touched = record_event(state, event)
    assert len(touched) == 1
    assert touched[0].state == IncidentState.SUSPECTED
    assert touched[0].threat_class == ThreatClass.RECONNAISSANCE


# 3. multi-event exfiltration pattern -> incident (CONFIRMED; full 4-indicator match)
def test_exfiltration_pattern_confirms_incident_only_once_all_four_signals_present():
    state = new_sentinel_state()
    owner = _owner()
    now = datetime.now(timezone.utc)
    _, _, t1 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.USB_CONNECTED, occurred_at=now))
    _, _, t2 = record_event(
        state,
        _event(owner_id=owner, event_type=SecurityEventType.PROCESS_STARTED, details={"process_known": False}, occurred_at=now + timedelta(seconds=1)),
    )
    _, _, t3 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.MASS_FILE_READ, occurred_at=now + timedelta(seconds=2)))
    assert t1 == [] and t2 == [] and t3 == [], "no incident until every required indicator has fired"
    _, _, t4 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNEXPECTED_EGRESS, occurred_at=now + timedelta(seconds=3)))
    assert len(t4) == 1
    assert t4[0].threat_class == ThreatClass.EXFILTRATION
    assert t4[0].state == IncidentState.CONFIRMED
    assert len(t4[0].evidence) == 4


# 4. ransomware pattern -> incident
def test_ransomware_pattern_confirms_incident():
    state = new_sentinel_state()
    owner = _owner()
    event = _event(
        owner_id=owner,
        event_type=SecurityEventType.MASS_FILE_WRITE,
        details={"high_entropy_output": True, "file_rename_burst": True},
    )
    _, _, touched = record_event(state, event)
    assert len(touched) == 1
    assert touched[0].threat_class == ThreatClass.RANSOMWARE
    assert touched[0].state == IncidentState.CONFIRMED


# 5. credential theft pattern -> incident
def test_credential_theft_pattern_confirms_incident():
    state = new_sentinel_state()
    owner = _owner()
    now = datetime.now(timezone.utc)
    _, _, t1 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.CREDENTIAL_READ_ATTEMPT, occurred_at=now))
    assert t1 == []
    _, _, t2 = record_event(
        state, _event(owner_id=owner, event_type=SecurityEventType.NEW_OUTBOUND_DESTINATION, occurred_at=now + timedelta(seconds=1))
    )
    assert len(t2) == 1
    assert t2[0].threat_class == ThreatClass.CREDENTIAL_THEFT
    assert t2[0].state == IncidentState.CONFIRMED


# 6. model tamper -> incident
def test_model_tampering_pattern_confirms_incident():
    state = new_sentinel_state()
    owner = _owner()
    event = _event(owner_id=owner, event_type=SecurityEventType.MODEL_CHANGED, details={"signature_mismatch": True})
    _, _, touched = record_event(state, event)
    assert len(touched) == 1
    assert touched[0].threat_class == ThreatClass.MODEL_TAMPERING
    assert touched[0].state == IncidentState.CONFIRMED

    # A model change WITHOUT a signature mismatch must not fire the pattern -- exact
    # detail-value match, not "any MODEL_CHANGED event" (evidence exists != evidence
    # supports claim, applied to correlation indicators).
    state2 = new_sentinel_state()
    benign = _event(owner_id=owner, event_type=SecurityEventType.MODEL_CHANGED, details={"signature_mismatch": False})
    _, _, touched2 = record_event(state2, benign)
    assert touched2 == []


# 7. canary touch -> high-confidence incident
def test_canary_touch_opens_high_confidence_incident_unconditionally():
    state = new_sentinel_state()
    owner = _owner()
    canary = register_canary(state, owner_id=owner, kind="fake_secret", subject_ref="canary_fixture_9f8a7b6c")
    event = build_canary_touch_event(canary, device_id="device-1")
    _, _, touched = record_event(state, event)
    assert len(touched) == 1
    assert touched[0].state == IncidentState.CONFIRMED
    assert touched[0].confidence == SecurityConfidence.HIGH
    assert touched[0].severity == SecuritySeverity.CRITICAL
    assert touched[0].threat_class == ThreatClass.CANARY_INTERACTION


def test_canary_subject_ref_rejects_real_looking_credential_shapes():
    owner = _owner()
    # Built via concatenation, never as one contiguous literal -- these are synthetic values
    # that only exist to prove the *rejection* path works, but a credential-shaped literal
    # sitting in source is exactly what tripped this session's earlier GitHub push-protection
    # incident (see canary.py module docstring), so the shape is assembled at runtime instead.
    bad_shapes = (
        "sk" + "_live_" + "9f8a7b6c5d4e3f2a1b0c9d8e",
        "sk" + "-" + "abcdef0123456789",
        "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIA" + "ABCDEFGHIJKLMNOP",
    )
    for bad in bad_shapes:
        with pytest.raises(ValueError):
            register_canary(new_sentinel_state(), owner_id=owner, kind="fake_secret", subject_ref=bad)
    # An explicit synthetic fixture is fine.
    canary = register_canary(new_sentinel_state(), owner_id=owner, kind="fake_secret", subject_ref="canary_fixture_abc123")
    assert canary.subject_ref == "canary_fixture_abc123"


# 8. duplicate event -> deduplicated
def test_duplicate_event_is_deduplicated_not_double_counted():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.unsigned_binary",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    now = datetime.now(timezone.utc)
    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="proc-1", occurred_at=now)
    _, r1, t1 = record_event(state, event)
    assert len(t1) == 1
    assert r1.deduplicated is False
    incident_id = t1[0].incident_id
    evidence_count_after_first = len(t1[0].evidence)

    duplicate = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="proc-1", occurred_at=now + timedelta(seconds=1))
    _, r2, t2 = record_event(state, duplicate)
    assert r2.deduplicated is True
    assert t2 == []
    incident = state.find_incident(incident_id)
    assert len(incident.evidence) == evidence_count_after_first, "a deduplicated event must not add new evidence"


# 9. cross-owner events -> never correlated
def test_cross_owner_events_are_never_correlated():
    state = new_sentinel_state()
    owner_a = _owner()
    owner_b = _owner()
    state.allow_cross_device_correlation = True  # even with the most permissive setting available
    now = datetime.now(timezone.utc)
    record_event(state, _event(owner_id=owner_a, event_type=SecurityEventType.CREDENTIAL_READ_ATTEMPT, occurred_at=now))
    _, _, touched = record_event(state, _event(owner_id=owner_b, event_type=SecurityEventType.NEW_OUTBOUND_DESTINATION, occurred_at=now + timedelta(seconds=1)))
    assert touched == [], "different owner_id must never contribute to another owner's correlation, regardless of any policy flag"


def test_cross_owner_three_check():
    """Three-check: confirm that if the owner_id filter were dropped from events_in_scope,
    the cross-owner event WOULD be included as a candidate -- proving the filter in the test
    above is doing real work, not vacuously passing because the pattern wouldn't have
    matched anyway."""
    from app.sentinel.correlation import CREDENTIAL_THEFT_PATTERN, events_in_scope

    owner_a = _owner()
    owner_b = _owner()
    now = datetime.now(timezone.utc)
    event_a = _event(owner_id=owner_a, event_type=SecurityEventType.CREDENTIAL_READ_ATTEMPT, occurred_at=now)
    event_b = _event(owner_id=owner_b, event_type=SecurityEventType.NEW_OUTBOUND_DESTINATION, occurred_at=now + timedelta(seconds=1))

    real_scope = events_in_scope(
        [event_a, event_b], owner_id=owner_b, device_id=event_b.subject.device_id, since=now, allow_cross_device=True
    )
    assert event_a not in real_scope, "the real owner_id filter must exclude owner_a's event from owner_b's scope"

    # Deliberately broken: no owner_id filter at all.
    broken_scope = [e for e in [event_a, event_b] if e.occurred_at >= now]
    assert event_a in broken_scope, "sanity: the broken version would have included the cross-owner event"
    from app.sentinel.correlation import pattern_full_match

    assert pattern_full_match(CREDENTIAL_THEFT_PATTERN, broken_scope) is not None, (
        "the broken (unfiltered) scope would have let the pattern fully match across owners -- "
        "proving the owner_id filter in events_in_scope is the thing actually preventing it"
    )


# 10. cross-device event -> only correlated when policy explicitly allows it (default: not correlated)
def test_cross_device_correlation_is_opt_in_and_off_by_default():
    state = new_sentinel_state()
    owner = _owner()
    now = datetime.now(timezone.utc)
    record_event(state, _event(owner_id=owner, device_id="laptop", event_type=SecurityEventType.CREDENTIAL_READ_ATTEMPT, occurred_at=now))
    _, _, touched_default = record_event(
        state, _event(owner_id=owner, device_id="phone", event_type=SecurityEventType.NEW_OUTBOUND_DESTINATION, occurred_at=now + timedelta(seconds=1))
    )
    assert touched_default == [], "default policy must not correlate across devices"

    state2 = new_sentinel_state()
    state2.allow_cross_device_correlation = True
    record_event(state2, _event(owner_id=owner, device_id="laptop", event_type=SecurityEventType.CREDENTIAL_READ_ATTEMPT, occurred_at=now))
    _, _, touched_allowed = record_event(
        state2, _event(owner_id=owner, device_id="phone", event_type=SecurityEventType.NEW_OUTBOUND_DESTINATION, occurred_at=now + timedelta(seconds=1))
    )
    assert len(touched_allowed) == 1, "explicitly opting in must allow cross-device correlation"


# 11. false-positive closeout -> proven NOT to widen authority/trust
def test_false_positive_closeout_is_scoped_and_never_widens_trust():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.a",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    _active_rule(
        state,
        rule_id="rule.b",
        event_types=frozenset({SecurityEventType.PRIVILEGE_ESCALATION_ATTEMPT}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.PRIVILEGE_ESCALATION,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    _, _, t_a1 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="subject-x"))
    _, _, t_a2 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="subject-y", details={"n": 1}))
    _, _, t_b1 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.PRIVILEGE_ESCALATION_ATTEMPT, severity=SecuritySeverity.HIGH, subject_ref="subject-z"))

    incident_a1_id = t_a1[0].incident_id
    incident_a2_before = copy.deepcopy(state.find_incident(t_a2[0].incident_id))
    incident_b1_before = copy.deepcopy(state.find_incident(t_b1[0].incident_id))
    rule_a_before = state.rule_registry.current_rule("rule.a")
    rule_b_before = state.rule_registry.current_rule("rule.b")

    closed = mark_false_positive(state, incident_id=incident_a1_id, reason="known internal dev tool, not malicious")

    assert closed.state == IncidentState.FALSE_POSITIVE
    # The OTHER incident from the SAME rule, on a different subject, must be untouched.
    incident_a2_after = state.find_incident(t_a2[0].incident_id)
    assert incident_a2_after.state == incident_a2_before.state
    assert incident_a2_after.severity == incident_a2_before.severity
    # A DIFFERENT rule's incident must be completely untouched.
    incident_b1_after = state.find_incident(t_b1[0].incident_id)
    assert incident_b1_after.state == incident_b1_before.state
    # Neither rule's lifecycle state changed.
    assert state.rule_registry.current_rule("rule.a").state == rule_a_before.state
    assert state.rule_registry.current_rule("rule.b").state == rule_b_before.state
    # A tuning candidate was recorded, scoped to the closed incident's own rule/subject.
    candidates = state.tuning_candidates_snapshot()
    assert len(candidates) == 1
    assert candidates[0].incident_id == incident_a1_id
    assert candidates[0].rule_or_pattern_id == "rule.a:subject-x"


def test_false_positive_scoping_three_check():
    """Three-check: deliberately break the scoping (make mark_false_positive close every
    incident sharing a rule_id) and confirm the assertions above would actually fail."""
    from app.sentinel import correlation as correlation_module
    from app.sentinel import service as service_module

    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.a",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    _, _, t1 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="subject-x"))
    _, _, t2 = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="subject-y", details={"n": 1}))

    def _broken_mark_false_positive(incidents, *, incident_id, reason):
        # Simulates the real bug this scoping guards against: grouping by rule_id ALONE
        # (ignoring subject_ref), which is exactly what source_id looked like before the fix
        # in correlation.apply_detection_results.
        target = correlation_module.find_incident(incidents, incident_id)
        target_rule_prefix = target.source_pattern_or_rule_id.split(":")[0]
        for incident in incidents.values():
            if incident.source_pattern_or_rule_id.split(":")[0] == target_rule_prefix:
                incident.state = IncidentState.FALSE_POSITIVE
                incident.false_positive_reason = reason
        return target

    original = service_module._mark_false_positive
    service_module._mark_false_positive = _broken_mark_false_positive
    try:
        service_module.mark_false_positive(state, incident_id=t1[0].incident_id, reason="test")
        broken_result = state.find_incident(t2[0].incident_id).state
    finally:
        service_module._mark_false_positive = original

    assert broken_result == IncidentState.FALSE_POSITIVE, (
        "the deliberately-broken version should have incorrectly widened the false-positive "
        "closeout to the sibling incident -- if it didn't, this three-check test itself is broken"
    )


# 12. incident survives serialization/reload
def test_state_survives_serialization_round_trip():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.unsigned_binary",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH))
    register_canary(state, owner_id=owner, kind="fake_secret", subject_ref="canary_fixture_roundtrip")

    snapshot = to_snapshot(state)
    restored = from_snapshot(snapshot)

    assert len(restored.incidents_snapshot()) == len(state.incidents_snapshot())
    assert len(restored.receipts_snapshot()) == len(state.receipts_snapshot())
    assert len(restored.events_snapshot()) == len(state.events_snapshot())
    assert len(restored.canaries_snapshot()) == len(state.canaries_snapshot())
    orig_incident = state.incidents_snapshot()[0]
    restored_incident = restored.incidents_snapshot()[0]
    assert restored_incident.incident_id == orig_incident.incident_id
    assert restored_incident.state == orig_incident.state
    assert restored_incident.threat_class == orig_incident.threat_class
    assert restored.rule_registry.current_rule("rule.unsigned_binary").state == RuleState.ACTIVE
    assert verify_event_chain_intact(restored)


# 13. rule rollback works
def test_rule_rollback_restores_prior_matching_behavior():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.tightening",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    tightened = new_detection_rule(
        rule_id="rule.tightening",
        version=2,
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        severity=SecuritySeverity.CRITICAL,
        confidence=SecurityConfidence.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        conditions={"min_event_severity": SecuritySeverity.CRITICAL},
    )
    publish_new_version(state, "rule.tightening", tightened)
    promote_rule(state, "rule.tightening", to_state=RuleState.TESTING)
    promote_rule(state, "rule.tightening", to_state=RuleState.VERIFIED)
    promote_rule(state, "rule.tightening", to_state=RuleState.ACTIVE)

    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p1")
    _, _, touched_tightened = record_event(state, event)
    assert touched_tightened == [], "the tightened v2 rule (min CRITICAL) must not match a HIGH-severity event"

    rollback_rule(state, "rule.tightening")
    event2 = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p2")
    _, _, touched_rolled_back = record_event(state, event2)
    assert len(touched_rolled_back) == 1, "after rollback, the original (v1) matching behavior must be restored"


# 14. revoked rule does not fire
def test_revoked_rule_never_fires_again():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.revocable",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p1")
    _, _, touched_before = record_event(state, event)
    assert len(touched_before) == 1

    revoke_rule(state, "rule.revocable")
    event2 = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p2")
    _, _, touched_after = record_event(state, event2)
    assert touched_after == []

    # Re-encountering the exact same (now-stale) event object again must still not fire.
    _, _, touched_repeat = record_event(state, _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p3"))
    assert touched_repeat == []

    with pytest.raises(SentinelRuleError):
        promote_rule(state, "rule.revocable", to_state=RuleState.ACTIVE)


def test_revoked_rule_three_check():
    """Three-check: confirm evaluate_event_against_registry would actually return a result
    for a revoked rule if the TRUSTED_RULE_STATES filter were removed -- proving the test
    above is a real regression guard, not a tautology."""
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.revocable2",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
    )
    revoke_rule(state, "rule.revocable2")
    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH)

    real_results = evaluate_event_against_registry(state.rule_registry, event)
    assert real_results == [], "revoked rule must not produce a DetectionResult through the real code path"

    # Deliberately bypass the trust-state gate to prove rule_matches_event's raw content
    # match WOULD have fired -- i.e. the guard in evaluate_event_against_registry is doing
    # real work, not vacuously passing.
    revoked_rule = state.rule_registry.current_rule("rule.revocable2")
    assert revoked_rule.state == RuleState.REVOKED
    assert rule_matches_event(revoked_rule, event) is True


# 15. unknown rule state (PROPOSED/TESTING) fails closed
def test_proposed_and_testing_rules_do_not_get_active_level_trust():
    state = new_sentinel_state()
    owner = _owner()
    rule = new_detection_rule(
        rule_id="rule.untested",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        threat_class=ThreatClass.RECONNAISSANCE,
        conditions={"min_event_severity": SecuritySeverity.HIGH},
    )
    propose_rule(state, rule)  # PROPOSED
    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p1")
    _, _, touched_proposed = record_event(state, event)
    assert touched_proposed == []
    assert evaluate_event_against_registry(state.rule_registry, event) == []

    promote_rule(state, "rule.untested", to_state=RuleState.TESTING)
    event2 = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p2")
    _, _, touched_testing = record_event(state, event2)
    assert touched_testing == []

    promote_rule(state, "rule.untested", to_state=RuleState.VERIFIED)
    event3 = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH, subject_ref="p3")
    _, _, touched_verified = record_event(state, event3)
    assert len(touched_verified) == 1, "VERIFIED must be trusted even before ACTIVE"


def test_rule_state_transitions_are_a_closed_table():
    state = new_sentinel_state()
    rule = new_detection_rule(
        rule_id="rule.transition",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        threat_class=ThreatClass.RECONNAISSANCE,
    )
    propose_rule(state, rule)
    with pytest.raises(SentinelRuleError):
        promote_rule(state, "rule.transition", to_state=RuleState.ACTIVE)  # cannot skip TESTING/VERIFIED
    promote_rule(state, "rule.transition", to_state=RuleState.TESTING)
    promote_rule(state, "rule.transition", to_state=RuleState.VERIFIED)
    promote_rule(state, "rule.transition", to_state=RuleState.ACTIVE)
    promote_rule(state, "rule.transition", to_state=RuleState.REVOKED)
    with pytest.raises(SentinelRuleError):
        promote_rule(state, "rule.transition", to_state=RuleState.ACTIVE)  # no transition out of REVOKED


# 16. Sentinel cannot directly execute a Guardian-shaped action
def test_sentinel_cannot_directly_execute_guardian_shaped_action():
    import inspect

    import app.sentinel as sentinel_pkg
    import app.sentinel.types as sentinel_types

    # No actual import statement pulling in app.guardian anywhere in the package's own
    # source (docstrings are allowed to mention "does not import app.guardian" -- this only
    # checks for a real `import`/`from` statement).
    import pathlib
    import re

    package_dir = pathlib.Path(sentinel_pkg.__file__).parent
    import_pattern = re.compile(r"^\s*(import app\.guardian|from app\.guardian)", re.MULTILINE)
    for py_file in package_dir.glob("*.py"):
        text = py_file.read_text()
        assert not import_pattern.search(text), f"{py_file.name} must not import app.guardian"

    # DefensiveActionRequest has no execute()/apply() method.
    assert not hasattr(sentinel_types.DefensiveActionRequest, "execute")
    assert not hasattr(sentinel_types.DefensiveActionRequest, "apply")
    request_fields = {f for f in dir(sentinel_types.DefensiveActionRequest) if not f.startswith("_")}
    assert "execute" not in request_fields and "apply" not in request_fields

    # build_defensive_action_request's signature has nothing shaped like a Guardian decision.
    params = inspect.signature(build_defensive_action_request).parameters
    assert not any("guardian" in name.lower() for name in params)


# 17. privacy-sensitive raw payload is rejected when attached to a SecurityEvent
def test_raw_content_detail_is_rejected_by_denylisted_key():
    owner = _owner()
    event = _event(
        owner_id=owner,
        event_type=SecurityEventType.MASS_FILE_READ,
        details={"raw_content": "the user's actual private document text goes here"},
    )
    with pytest.raises(SentinelPrivacyViolation):
        record_event(new_sentinel_state(), event)


def test_long_free_text_detail_is_rejected_even_under_an_innocuous_key():
    owner = _owner()
    long_text = "this looks like a real sentence of private conversation content " * 3
    with pytest.raises(SentinelPrivacyViolation):
        record_event(new_sentinel_state(), _event(owner_id=owner, event_type=SecurityEventType.MASS_FILE_READ, details={"note": long_text}))


def test_structured_short_scalar_details_are_accepted():
    owner = _owner()
    state = new_sentinel_state()
    event = _event(
        owner_id=owner,
        event_type=SecurityEventType.MASS_FILE_READ,
        details={"file_count": 5000, "path_class": "documents", "high_entropy_output": True},
    )
    _, receipt, _ = record_event(state, event)
    assert receipt.deduplicated is False  # got past validation and was recorded


# --- Defensive autonomy != general autonomy: PreauthorizedDefense bounds. -----------------


def test_preauthorized_defense_rejects_wildcard_scope():
    with pytest.raises(ValueError):
        require_bounded_preauthorization(
            PreauthorizedDefense(
                allowed_action=DefensiveAction.FREEZE_PROCESS,
                scope_hint="*",
                min_severity=SecuritySeverity.HIGH,
                min_confidence=SecurityConfidence.HIGH,
                time_to_damage_class="minutes",
                owner_response_timeout_seconds=300,
                max_containment_duration_seconds=3600,
                rollback_conditions="owner reviews within 24h",
            )
        )


def test_preauthorized_defense_rejects_unbounded_duration():
    with pytest.raises(ValueError):
        require_bounded_preauthorization(
            PreauthorizedDefense(
                allowed_action=DefensiveAction.FREEZE_PROCESS,
                scope_hint="process:unsigned_binary",
                min_severity=SecuritySeverity.HIGH,
                min_confidence=SecurityConfidence.HIGH,
                time_to_damage_class="minutes",
                owner_response_timeout_seconds=0,
                max_containment_duration_seconds=3600,
                rollback_conditions="owner reviews within 24h",
            )
        )


def test_bounded_preauthorized_defense_is_accepted_and_covers_matching_incident():
    state = new_sentinel_state()
    owner = _owner()
    canary = register_canary(state, owner_id=owner, kind="fake_secret", subject_ref="canary_fixture_defense")
    _, _, touched = record_event(state, build_canary_touch_event(canary, device_id="device-1"))
    incident = touched[0]

    preauth = PreauthorizedDefense(
        allowed_action=DefensiveAction.LOCK_VAULT,
        scope_hint="owner:canary_response",
        min_severity=SecuritySeverity.HIGH,
        min_confidence=SecurityConfidence.MEDIUM,
        time_to_damage_class="seconds",
        owner_response_timeout_seconds=60,
        max_containment_duration_seconds=1800,
        rollback_conditions="owner explicitly re-enables Vault access",
    )
    require_bounded_preauthorization(preauth)  # does not raise

    request = build_defensive_action_request(
        incident, action=DefensiveAction.LOCK_VAULT, scope_hint="owner:canary_response", reason="canary touched", preauthorized_by=preauth
    )
    assert request.action == DefensiveAction.LOCK_VAULT
    assert request.preauthorized_by is preauth
    assert not hasattr(request, "execute")


def test_defensive_action_request_rejects_preauthorization_that_does_not_cover_it():
    state = new_sentinel_state()
    owner = _owner()
    _active_rule(
        state,
        rule_id="rule.low_conf",
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        min_severity=SecuritySeverity.HIGH,
        threat_class=ThreatClass.RECONNAISSANCE,
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.LOW,
    )
    event = _event(owner_id=owner, event_type=SecurityEventType.UNSIGNED_BINARY_EXECUTED, severity=SecuritySeverity.HIGH)
    _, _, touched = record_event(state, event)
    incident = touched[0]

    preauth_requiring_high_confidence = PreauthorizedDefense(
        allowed_action=DefensiveAction.FREEZE_PROCESS,
        scope_hint="process:unsigned_binary",
        min_severity=SecuritySeverity.HIGH,
        min_confidence=SecurityConfidence.HIGH,  # incident only has LOW confidence
        time_to_damage_class="minutes",
        owner_response_timeout_seconds=300,
        max_containment_duration_seconds=3600,
        rollback_conditions="owner reviews within 24h",
    )
    with pytest.raises(ValueError):
        build_defensive_action_request(
            incident,
            action=DefensiveAction.FREEZE_PROCESS,
            scope_hint="process:unsigned_binary",
            reason="unsigned binary executed",
            preauthorized_by=preauth_requiring_high_confidence,
        )


# --- Threat pack. ------------------------------------------------------------------------


def test_example_threat_pack_loads_offline_and_validates():
    from app.sentinel import EXAMPLE_TEST_PACK, install_threat_pack, load_threat_pack

    validated = load_threat_pack(EXAMPLE_TEST_PACK)
    assert validated.pack_id == "example-test-pack"

    state = new_sentinel_state()
    install_threat_pack(state, EXAMPLE_TEST_PACK)
    installed_rule = state.rule_registry.current_rule("test.pack.unsigned_binary.v1")
    assert installed_rule is not None
    assert installed_rule.state == RuleState.PROPOSED, "installing a pack must not itself grant trust"
