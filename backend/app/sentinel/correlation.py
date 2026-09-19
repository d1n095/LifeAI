"""Sentinel -- incident correlation (Stage V2-D3).

Deterministic, bounded correlation -- no LLM required. CORRELATION != CERTAINTY: every
SecurityIncident tracks its own evidence, confidence, a rationale for why it exists, and
clearing_conditions describing what would clear it. MainAI may later enrich/explain an
incident; it never gets to invent the underlying evidence -- that always traces back to a
real DetectionResult or CorrelationPattern match recorded here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.sentinel.types import (
    CONFIDENCE_ORDER,
    SEVERITY_ORDER,
    CorrelationPattern,
    DetectionResult,
    IncidentEvidence,
    IncidentState,
    RequiredIndicator,
    SecurityConfidence,
    SecurityEvent,
    SecurityEventType,
    SecurityIncident,
    SecuritySeverity,
    ThreatClass,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Default correlation patterns (the four founder-specified examples). -----------------

EXFILTRATION_PATTERN = CorrelationPattern(
    pattern_id="pattern.exfiltration.usb_process_read_egress",
    threat_class=ThreatClass.EXFILTRATION,
    required_indicators=(
        RequiredIndicator(SecurityEventType.USB_CONNECTED),
        RequiredIndicator(SecurityEventType.PROCESS_STARTED, {"process_known": False}),
        RequiredIndicator(SecurityEventType.MASS_FILE_READ),
        RequiredIndicator(SecurityEventType.UNEXPECTED_EGRESS),
    ),
    window_seconds=600,
    severity=SecuritySeverity.CRITICAL,
    confidence_on_full_match=SecurityConfidence.HIGH,
    rationale=(
        "unknown USB device connected, an unrecognized process started, a mass file read "
        "followed, then egress occurred -- the classic exfiltration shape"
    ),
    clearing_conditions=(
        "owner confirms the USB device and process are expected, or the egress destination "
        "is added to the trusted destination list"
    ),
)

CREDENTIAL_THEFT_PATTERN = CorrelationPattern(
    pattern_id="pattern.credential_theft.read_then_new_destination",
    threat_class=ThreatClass.CREDENTIAL_THEFT,
    required_indicators=(
        RequiredIndicator(SecurityEventType.CREDENTIAL_READ_ATTEMPT),
        RequiredIndicator(SecurityEventType.NEW_OUTBOUND_DESTINATION),
    ),
    window_seconds=300,
    severity=SecuritySeverity.HIGH,
    confidence_on_full_match=SecurityConfidence.HIGH,
    rationale="a credential read attempt was followed by an outbound connection to a never-seen-before destination",
    clearing_conditions="owner confirms the destination is expected, or the credential read is attributed to a trusted local process",
)

RANSOMWARE_PATTERN = CorrelationPattern(
    pattern_id="pattern.ransomware.mass_write_entropy_rename_burst",
    threat_class=ThreatClass.RANSOMWARE,
    required_indicators=(
        RequiredIndicator(SecurityEventType.MASS_FILE_WRITE, {"high_entropy_output": True}),
        RequiredIndicator(SecurityEventType.MASS_FILE_WRITE, {"file_rename_burst": True}),
    ),
    window_seconds=120,
    severity=SecuritySeverity.CRITICAL,
    confidence_on_full_match=SecurityConfidence.HIGH,
    rationale="mass file writes with high-entropy output and a file rename burst -- the signature shape of ransomware",
    clearing_conditions="owner confirms a legitimate bulk operation (backup software, archive tool) caused the writes",
)

MODEL_TAMPERING_PATTERN = CorrelationPattern(
    pattern_id="pattern.model_tampering.signature_mismatch",
    threat_class=ThreatClass.MODEL_TAMPERING,
    required_indicators=(RequiredIndicator(SecurityEventType.MODEL_CHANGED, {"signature_mismatch": True}),),
    window_seconds=60,
    severity=SecuritySeverity.CRITICAL,
    confidence_on_full_match=SecurityConfidence.HIGH,
    rationale="a local model file changed and its signature no longer matches its expected provenance",
    clearing_conditions="owner explicitly approves the new model version and provenance is re-established",
)

DEFAULT_CORRELATION_PATTERNS: tuple[CorrelationPattern, ...] = (
    EXFILTRATION_PATTERN,
    CREDENTIAL_THEFT_PATTERN,
    RANSOMWARE_PATTERN,
    MODEL_TAMPERING_PATTERN,
)

_STATE_RANK = {
    IncidentState.OBSERVED: 0,
    IncidentState.SUSPECTED: 1,
    IncidentState.CONFIRMED: 2,
    IncidentState.CONTAINED: 3,
    IncidentState.RECOVERING: 4,
    IncidentState.RESOLVED: 5,
    IncidentState.FALSE_POSITIVE: 5,
}


def _indicator_satisfied(indicator: RequiredIndicator, event: SecurityEvent) -> bool:
    if event.event_type != indicator.event_type:
        return False
    for key, expected in indicator.required_details.items():
        if event.details.get(key) != expected:
            return False
    return True


def pattern_full_match(pattern: CorrelationPattern, candidate_events: list[SecurityEvent]) -> list[SecurityEvent] | None:
    """Every indicator must be satisfied by at least one candidate event (the same event may
    satisfy more than one indicator). Returns the satisfying events (one per indicator,
    index-aligned) or None if any indicator is unsatisfied."""
    satisfying: list[SecurityEvent] = []
    for indicator in pattern.required_indicators:
        match = next((e for e in candidate_events if _indicator_satisfied(indicator, e)), None)
        if match is None:
            return None
        satisfying.append(match)
    return satisfying


def events_in_scope(
    all_events: list[SecurityEvent],
    *,
    owner_id: uuid.UUID,
    device_id: str,
    since: datetime,
    allow_cross_device: bool,
) -> list[SecurityEvent]:
    """Cross-owner events are NEVER included here, unconditionally -- there is no flag that
    relaxes the owner_id filter. Cross-device inclusion is the only thing `allow_cross_device`
    controls."""
    return [
        e
        for e in all_events
        if e.subject.owner_id == owner_id
        and (allow_cross_device or e.subject.device_id == device_id)
        and e.occurred_at >= since
    ]


def _incident_key(owner_id: uuid.UUID, device_id: str, source_id: str) -> tuple:
    return (str(owner_id), device_id, source_id)


def find_incident(incidents: dict[tuple, SecurityIncident], incident_id: uuid.UUID) -> SecurityIncident | None:
    return next((i for i in incidents.values() if i.incident_id == incident_id), None)


def open_or_update_incident(
    incidents: dict[tuple, SecurityIncident],
    *,
    owner_id: uuid.UUID,
    device_id: str,
    source_id: str,
    threat_class: ThreatClass,
    target_state: IncidentState,
    severity: SecuritySeverity,
    confidence: SecurityConfidence,
    rationale: str,
    clearing_conditions: str,
    new_evidence: list[IncidentEvidence],
) -> SecurityIncident:
    """Keyed by (owner, device, source_id) so repeated firings of the same rule/pattern on
    the same subject accumulate evidence on one incident rather than spawning duplicates. A
    RESOLVED/FALSE_POSITIVE incident is never silently reopened by this function -- a fresh
    firing after that point starts a brand-new incident, so the earlier disposition stays
    intact and auditable."""
    key = _incident_key(owner_id, device_id, source_id)
    existing = incidents.get(key)
    if existing is not None and existing.state not in (IncidentState.RESOLVED, IncidentState.FALSE_POSITIVE):
        existing.evidence.extend(new_evidence)
        if SEVERITY_ORDER[severity] > SEVERITY_ORDER[existing.severity]:
            existing.severity = severity
        if CONFIDENCE_ORDER[confidence] > CONFIDENCE_ORDER[existing.confidence]:
            existing.confidence = confidence
        if _STATE_RANK[target_state] > _STATE_RANK[existing.state]:
            existing.state = target_state
        existing.updated_at = _utcnow()
        return existing

    incident = SecurityIncident(
        incident_id=uuid.uuid4(),
        threat_class=threat_class,
        state=target_state,
        severity=severity,
        confidence=confidence,
        owner_id=owner_id,
        device_id=device_id,
        evidence=list(new_evidence),
        counter_evidence=[],
        rationale=rationale,
        clearing_conditions=clearing_conditions,
        opened_at=_utcnow(),
        updated_at=_utcnow(),
        source_pattern_or_rule_id=source_id,
    )
    incidents[key] = incident
    return incident


def incident_state_for_detection_result(result: DetectionResult) -> IncidentState:
    if result.severity == SecuritySeverity.CRITICAL and result.confidence == SecurityConfidence.HIGH:
        return IncidentState.CONFIRMED
    return IncidentState.SUSPECTED


def apply_detection_results(
    incidents: dict[tuple, SecurityIncident],
    *,
    event: SecurityEvent,
    results: list[DetectionResult],
) -> list[SecurityIncident]:
    """Single-event, rule-based incident opening. Only HIGH/CRITICAL results open/update an
    incident -- LOW/MEDIUM/INFO results are still recorded on the event receipt but never by
    themselves open one (matches 'single benign event -> no incident')."""
    touched: list[SecurityIncident] = []
    for result in results:
        if SEVERITY_ORDER[result.severity] < SEVERITY_ORDER[SecuritySeverity.HIGH]:
            continue
        incident = open_or_update_incident(
            incidents,
            owner_id=event.subject.owner_id,
            device_id=event.subject.device_id,
            # Scoped by subject_ref too -- two different subjects triggering the same rule
            # must never collapse into one incident (that would make a false-positive
            # closeout on one subject silently affect the other's incident record).
            source_id=f"{result.rule_id}:{event.subject.subject_ref}",
            threat_class=result.threat_class,
            target_state=incident_state_for_detection_result(result),
            severity=result.severity,
            confidence=result.confidence,
            rationale=f"rule {result.rule_id} matched event {event.event_id} at {result.severity.value} severity",
            clearing_conditions=(
                "owner reviews and marks the underlying rule firing as a false positive, "
                "or the subject is confirmed legitimate"
            ),
            new_evidence=[
                IncidentEvidence(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    weight=1.0,
                    note=f"matched rule {result.rule_id} v{result.rule_version}",
                )
            ],
        )
        touched.append(incident)
    return touched


def apply_correlation_patterns(
    incidents: dict[tuple, SecurityIncident],
    *,
    event: SecurityEvent,
    all_events: list[SecurityEvent],
    patterns: tuple[CorrelationPattern, ...],
    allow_cross_device_correlation: bool,
) -> list[SecurityIncident]:
    """Only opens/updates an incident when the just-recorded `event` is itself one of the
    events satisfying the now-fully-matched pattern -- prevents re-touching an incident on
    every subsequent, unrelated event just because the pattern happened to still be
    satisfied by older events still inside the window."""
    touched: list[SecurityIncident] = []
    for pattern in patterns:
        since = event.occurred_at - timedelta(seconds=pattern.window_seconds)
        candidates = events_in_scope(
            all_events,
            owner_id=event.subject.owner_id,
            device_id=event.subject.device_id,
            since=since,
            allow_cross_device=allow_cross_device_correlation or pattern.allow_cross_device,
        )
        satisfying = pattern_full_match(pattern, candidates)
        if satisfying is None or event not in satisfying:
            continue
        incident = open_or_update_incident(
            incidents,
            owner_id=event.subject.owner_id,
            device_id=event.subject.device_id,
            source_id=pattern.pattern_id,
            threat_class=pattern.threat_class,
            target_state=IncidentState.CONFIRMED,
            severity=pattern.severity,
            confidence=pattern.confidence_on_full_match,
            rationale=pattern.rationale,
            clearing_conditions=pattern.clearing_conditions,
            new_evidence=[
                IncidentEvidence(
                    event_id=e.event_id,
                    event_type=e.event_type,
                    weight=1.0 / len(pattern.required_indicators),
                    note=f"satisfied one indicator of pattern {pattern.pattern_id}",
                )
                for e in satisfying
            ],
        )
        touched.append(incident)
    return touched


def mark_false_positive(
    incidents: dict[tuple, SecurityIncident],
    *,
    incident_id: uuid.UUID,
    reason: str,
) -> SecurityIncident:
    """FALSE POSITIVE CORRECTION != GLOBAL TRUST GRANT: mutates ONLY the one incident named
    by incident_id -- nothing in this function's signature can reach any other incident, any
    rule/pattern registry, or any authority/trust state."""
    incident = find_incident(incidents, incident_id)
    if incident is None:
        raise ValueError(f"unknown incident_id {incident_id}")
    incident.state = IncidentState.FALSE_POSITIVE
    incident.false_positive_reason = reason
    incident.updated_at = _utcnow()
    return incident
