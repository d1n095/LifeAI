"""Sentinel -- top-level state and orchestration (Stages V2-D1..D4).

Standalone, isolated, NOT imported by any production runtime path, and does NOT import
app.guardian or app.privacy_boundary. This module is the single entry point a caller (a
future adapter, or a test) uses: `record_event()` is where privacy validation, dedup,
rule evaluation, correlation, canary handling, retention, and receipt-chain recording all
happen -- in that order, every time, for every event.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.sentinel.canary import register_canary as _register_canary
from app.sentinel.correlation import (
    DEFAULT_CORRELATION_PATTERNS,
    apply_correlation_patterns,
    apply_detection_results,
    find_incident,
    mark_false_positive as _mark_false_positive,
    open_or_update_incident,
)
from app.sentinel.rules import (
    RuleRegistry,
    evaluate_event_against_registry,
    promote_rule as _promote_rule,
    propose_rule as _propose_rule,
    publish_new_version as _publish_new_version,
    revoke_rule as _revoke_rule,
    rollback_rule as _rollback_rule,
)
from app.sentinel.threat_pack import load_threat_pack
from app.sentinel.types import (
    CanaryResource,
    CorrelationPattern,
    DetectionResult,
    DetectionRule,
    EventReceipt,
    IncidentEvidence,
    IncidentState,
    RetentionPolicy,
    RuleState,
    RuleTuningCandidate,
    SecurityConfidence,
    SecurityEvent,
    SecurityEventType,
    SecurityIncident,
    SecurityKnowledgePack,
    SecuritySeverity,
    SecuritySource,
    SecuritySubject,
    SentinelPrivacyViolation,
    SentinelRuleError,
    ThreatClass,
)

_GENESIS_HASH = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Privacy validation: SPECIALIZATION != SURVEILLANCE. ---------------------------------

_DENYLISTED_DETAIL_KEYS = frozenset(
    {
        "raw_content",
        "raw_text",
        "document_text",
        "conversation",
        "message_body",
        "body",
        "content",
        "transcript",
        "private_note",
        "raw_document",
        "chat_history",
        "full_text",
    }
)
_MAX_STRING_DETAIL_LENGTH = 120
_MAX_SEQUENCE_DETAIL_LENGTH = 20


def _validate_detail_scalar(value: object) -> None:
    if isinstance(value, str):
        if len(value) > _MAX_STRING_DETAIL_LENGTH:
            raise SentinelPrivacyViolation(
                f"detail string value is {len(value)} chars, longer than the "
                f"{_MAX_STRING_DETAIL_LENGTH}-char limit for a classified signal field -- looks "
                "like raw content, not a hash/class/category value"
            )
    elif isinstance(value, (int, float, bool)) or value is None:
        pass
    else:
        raise SentinelPrivacyViolation(
            f"unsupported SecurityEvent.details value type {type(value).__name__!r} -- details "
            "must hold pre-classified scalar fields only (hash, id, category, bool), never a "
            "nested raw blob"
        )


def _validate_details(details: dict) -> None:
    for key, value in details.items():
        if key.lower() in _DENYLISTED_DETAIL_KEYS:
            raise SentinelPrivacyViolation(
                f"detail key {key!r} looks like a raw-content field, not a classified signal field"
            )
        if isinstance(value, (list, tuple)):
            if len(value) > _MAX_SEQUENCE_DETAIL_LENGTH:
                raise SentinelPrivacyViolation(
                    f"detail sequence under key {key!r} has {len(value)} items -- looks like raw "
                    "content, not a classified field"
                )
            for item in value:
                _validate_detail_scalar(item)
        else:
            _validate_detail_scalar(value)


# --- State. --------------------------------------------------------------------------


@dataclass
class SentinelState:
    """Mutable Sentinel state. Every collection is exposed only via a `*_snapshot()` method
    (a copy) -- there is no public method that removes or mutates an existing receipt or
    event, matching Guardian's 'immutable event receipts' discipline."""

    rule_registry: RuleRegistry = field(default_factory=RuleRegistry)
    correlation_patterns: tuple[CorrelationPattern, ...] = field(default_factory=lambda: DEFAULT_CORRELATION_PATTERNS)
    retention_policy: RetentionPolicy = field(
        default_factory=lambda: RetentionPolicy(max_event_age_seconds=7 * 86400, max_events_retained=5000)
    )
    allow_cross_device_correlation: bool = False
    dedup_window_seconds: int = 30
    _events: list[SecurityEvent] = field(default_factory=list)
    _dedup_index: dict[str, datetime] = field(default_factory=dict)
    _incidents: dict[tuple, SecurityIncident] = field(default_factory=dict)
    _receipts: list[EventReceipt] = field(default_factory=list)
    _canaries: dict[str, CanaryResource] = field(default_factory=dict)
    _tuning_candidates: list[RuleTuningCandidate] = field(default_factory=list)

    def incidents_snapshot(self) -> tuple[SecurityIncident, ...]:
        return tuple(self._incidents.values())

    def receipts_snapshot(self) -> tuple[EventReceipt, ...]:
        return tuple(self._receipts)

    def events_snapshot(self) -> tuple[SecurityEvent, ...]:
        return tuple(self._events)

    def canaries_snapshot(self) -> tuple[CanaryResource, ...]:
        return tuple(self._canaries.values())

    def tuning_candidates_snapshot(self) -> tuple[RuleTuningCandidate, ...]:
        return tuple(self._tuning_candidates)

    def find_incident(self, incident_id: uuid.UUID) -> SecurityIncident | None:
        return find_incident(self._incidents, incident_id)


def new_sentinel_state() -> SentinelState:
    return SentinelState()


# --- Event fingerprinting (dedup). -----------------------------------------------------


def _event_fingerprint(event: SecurityEvent) -> str:
    payload = {
        "event_type": event.event_type.value,
        "owner_id": str(event.subject.owner_id),
        "device_id": event.subject.device_id,
        "subject_kind": event.subject.subject_kind,
        "subject_ref": event.subject.subject_ref,
        "details": {k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in event.details.items()},
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --- Event receipts: append-only, hash-chained. -----------------------------------------


def _receipt_hash(receipt: EventReceipt) -> str:
    payload = {
        "receipt_id": str(receipt.receipt_id),
        "event_id": str(receipt.event_id),
        "event_type": receipt.event_type.value,
        "deduplicated": receipt.deduplicated,
        "detection_results": [
            {
                "rule_id": r.rule_id,
                "rule_version": r.rule_version,
                "matched": r.matched,
                "rule_state_at_evaluation": r.rule_state_at_evaluation.value,
                "threat_class": r.threat_class.value,
                "severity": r.severity.value,
                "confidence": r.confidence.value,
            }
            for r in receipt.detection_results
        ],
        "incident_ids_touched": [str(i) for i in receipt.incident_ids_touched],
        "prev_hash": receipt.prev_hash,
        "created_at": receipt.created_at.isoformat(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _record_event_receipt(
    state: SentinelState,
    *,
    event: SecurityEvent,
    deduplicated: bool,
    results: list[DetectionResult],
    incident_ids: list[uuid.UUID],
) -> EventReceipt:
    prev_hash = state._receipts[-1].this_hash if state._receipts else _GENESIS_HASH
    receipt = EventReceipt(
        receipt_id=uuid.uuid4(),
        event_id=event.event_id,
        event_type=event.event_type,
        deduplicated=deduplicated,
        detection_results=tuple(results),
        incident_ids_touched=tuple(incident_ids),
        prev_hash=prev_hash,
    )
    receipt.this_hash = _receipt_hash(receipt)
    state._receipts.append(receipt)
    return receipt


def verify_event_chain_intact(state: SentinelState) -> bool:
    prev = _GENESIS_HASH
    for receipt in state._receipts:
        if receipt.prev_hash != prev:
            return False
        if _receipt_hash(receipt) != receipt.this_hash:
            return False
        prev = receipt.this_hash
    return True


# --- Retention. ------------------------------------------------------------------------


def _enforce_retention(state: SentinelState) -> None:
    cutoff = _utcnow() - timedelta(seconds=state.retention_policy.max_event_age_seconds)
    state._events = [e for e in state._events if e.occurred_at >= cutoff]
    overflow = len(state._events) - state.retention_policy.max_events_retained
    if overflow > 0:
        state._events = state._events[overflow:]


# --- Event recording: the single entry point. --------------------------------------------


def record_event(state: SentinelState, event: SecurityEvent) -> tuple[SecurityEvent, EventReceipt, list[SecurityIncident]]:
    """Validate -> dedup -> (canary short-circuit | rule evaluation + correlation) ->
    receipt -> retention, in that order, every time."""
    _validate_details(event.details)

    fingerprint = _event_fingerprint(event)
    last_seen = state._dedup_index.get(fingerprint)
    deduplicated = last_seen is not None and abs((event.occurred_at - last_seen).total_seconds()) <= state.dedup_window_seconds

    results: list[DetectionResult] = []
    touched: list[SecurityIncident] = []

    if not deduplicated:
        state._dedup_index[fingerprint] = event.occurred_at
        state._events.append(event)

        if event.event_type == SecurityEventType.CANARY_TOUCHED:
            # CANARY_TOUCHED bypasses the rule registry entirely -- a canary touch is an
            # unconditional, high-confidence signal by definition (see canary.py).
            incident = open_or_update_incident(
                state._incidents,
                owner_id=event.subject.owner_id,
                device_id=event.subject.device_id,
                source_id=f"canary:{event.subject.subject_ref}",
                threat_class=ThreatClass.CANARY_INTERACTION,
                target_state=IncidentState.CONFIRMED,
                severity=SecuritySeverity.CRITICAL,
                confidence=SecurityConfidence.HIGH,
                rationale=(
                    f"canary resource {event.subject.subject_ref} was touched -- canaries are "
                    "never touched in normal operation"
                ),
                clearing_conditions="owner confirms the canary access was an authorized test/drill",
                new_evidence=[
                    IncidentEvidence(
                        event_id=event.event_id, event_type=event.event_type, weight=1.0, note="canary touched"
                    )
                ],
            )
            touched.append(incident)
        else:
            results = evaluate_event_against_registry(state.rule_registry, event)
            touched.extend(apply_detection_results(state._incidents, event=event, results=results))
            touched.extend(
                apply_correlation_patterns(
                    state._incidents,
                    event=event,
                    all_events=state._events,
                    patterns=state.correlation_patterns,
                    allow_cross_device_correlation=state.allow_cross_device_correlation,
                )
            )

    seen_ids: set[uuid.UUID] = set()
    deduped_touched: list[SecurityIncident] = []
    for incident in touched:
        if incident.incident_id not in seen_ids:
            seen_ids.add(incident.incident_id)
            deduped_touched.append(incident)

    receipt = _record_event_receipt(
        state, event=event, deduplicated=deduplicated, results=results, incident_ids=[i.incident_id for i in deduped_touched]
    )
    _enforce_retention(state)
    return event, receipt, deduped_touched


# --- False positive workflow. ------------------------------------------------------------


def mark_false_positive(state: SentinelState, *, incident_id: uuid.UUID, reason: str) -> SecurityIncident:
    """FALSE POSITIVE CORRECTION != GLOBAL TRUST GRANT: closes out exactly one incident and
    records a scoped, NOT-auto-applied RuleTuningCandidate. Nothing here touches the rule
    registry or any other incident."""
    incident = _mark_false_positive(state._incidents, incident_id=incident_id, reason=reason)
    candidate = RuleTuningCandidate(
        candidate_id=uuid.uuid4(),
        rule_or_pattern_id=incident.source_pattern_or_rule_id,
        subject_ref=incident.device_id,
        incident_id=incident.incident_id,
        reason=reason,
    )
    state._tuning_candidates.append(candidate)
    return incident


# --- Rule lifecycle wrappers. ------------------------------------------------------------


def propose_rule(state: SentinelState, rule: DetectionRule) -> DetectionRule:
    return _propose_rule(state.rule_registry, rule)


def promote_rule(state: SentinelState, rule_id: str, *, to_state: RuleState, verified_at: datetime | None = None) -> DetectionRule:
    return _promote_rule(state.rule_registry, rule_id, to_state=to_state, verified_at=verified_at)


def revoke_rule(state: SentinelState, rule_id: str) -> DetectionRule:
    return _revoke_rule(state.rule_registry, rule_id)


def publish_new_version(state: SentinelState, rule_id: str, new_rule: DetectionRule) -> DetectionRule:
    return _publish_new_version(state.rule_registry, rule_id, new_rule)


def rollback_rule(state: SentinelState, rule_id: str) -> DetectionRule:
    return _rollback_rule(state.rule_registry, rule_id)


# --- Canary. -------------------------------------------------------------------------


def register_canary(state: SentinelState, *, owner_id: uuid.UUID, kind: str, subject_ref: str) -> CanaryResource:
    canary = _register_canary(owner_id=owner_id, kind=kind, subject_ref=subject_ref)
    state._canaries[subject_ref] = canary
    return canary


# --- Threat pack installation. -----------------------------------------------------------


def install_threat_pack(state: SentinelState, pack: SecurityKnowledgePack) -> SecurityKnowledgePack:
    """Validates the pack (offline, no network/filesystem access -- see threat_pack.py) and
    proposes each rule it ships. Proposing still leaves every rule in PROPOSED -- installing
    a pack is never itself sufficient to make its rules trusted (NEW RULE != TRUSTED RULE
    applies here too)."""
    validated = load_threat_pack(pack)
    for rule in validated.detection_rules:
        if state.rule_registry.current_rule(rule.rule_id) is None:
            _propose_rule(state.rule_registry, rule)
    return validated


# --- Serialization round-trip. ------------------------------------------------------------


def _ser_event(e: SecurityEvent) -> dict:
    return {
        "event_id": str(e.event_id),
        "event_type": e.event_type.value,
        "severity": e.severity.value,
        "confidence": e.confidence.value,
        "subject": {
            "owner_id": str(e.subject.owner_id),
            "device_id": e.subject.device_id,
            "subject_kind": e.subject.subject_kind,
            "subject_ref": e.subject.subject_ref,
        },
        "source": {
            "adapter_name": e.source.adapter_name,
            "adapter_version": e.source.adapter_version,
            "collected_at": e.source.collected_at.isoformat(),
        },
        "occurred_at": e.occurred_at.isoformat(),
        "correlation_id": str(e.correlation_id),
        "parent_event_id": str(e.parent_event_id) if e.parent_event_id else None,
        "details": dict(e.details),
    }


def _deser_event(d: dict) -> SecurityEvent:
    return SecurityEvent(
        event_id=uuid.UUID(d["event_id"]),
        event_type=SecurityEventType(d["event_type"]),
        severity=SecuritySeverity(d["severity"]),
        confidence=SecurityConfidence(d["confidence"]),
        subject=SecuritySubject(
            owner_id=uuid.UUID(d["subject"]["owner_id"]),
            device_id=d["subject"]["device_id"],
            subject_kind=d["subject"]["subject_kind"],
            subject_ref=d["subject"]["subject_ref"],
        ),
        source=SecuritySource(
            adapter_name=d["source"]["adapter_name"],
            adapter_version=d["source"]["adapter_version"],
            collected_at=datetime.fromisoformat(d["source"]["collected_at"]),
        ),
        occurred_at=datetime.fromisoformat(d["occurred_at"]),
        correlation_id=uuid.UUID(d["correlation_id"]),
        parent_event_id=uuid.UUID(d["parent_event_id"]) if d["parent_event_id"] else None,
        details=dict(d["details"]),
    )


def _ser_detection_result(r: DetectionResult) -> dict:
    return {
        "rule_id": r.rule_id,
        "rule_version": r.rule_version,
        "event_id": str(r.event_id),
        "matched": r.matched,
        "rule_state_at_evaluation": r.rule_state_at_evaluation.value,
        "threat_class": r.threat_class.value,
        "severity": r.severity.value,
        "confidence": r.confidence.value,
    }


def _deser_detection_result(d: dict) -> DetectionResult:
    return DetectionResult(
        rule_id=d["rule_id"],
        rule_version=d["rule_version"],
        event_id=uuid.UUID(d["event_id"]),
        matched=d["matched"],
        rule_state_at_evaluation=RuleState(d["rule_state_at_evaluation"]),
        threat_class=ThreatClass(d["threat_class"]),
        severity=SecuritySeverity(d["severity"]),
        confidence=SecurityConfidence(d["confidence"]),
    )


def _ser_receipt(r: EventReceipt) -> dict:
    return {
        "receipt_id": str(r.receipt_id),
        "event_id": str(r.event_id),
        "event_type": r.event_type.value,
        "deduplicated": r.deduplicated,
        "detection_results": [_ser_detection_result(x) for x in r.detection_results],
        "incident_ids_touched": [str(i) for i in r.incident_ids_touched],
        "prev_hash": r.prev_hash,
        "this_hash": r.this_hash,
        "created_at": r.created_at.isoformat(),
    }


def _deser_receipt(d: dict) -> EventReceipt:
    return EventReceipt(
        receipt_id=uuid.UUID(d["receipt_id"]),
        event_id=uuid.UUID(d["event_id"]),
        event_type=SecurityEventType(d["event_type"]),
        deduplicated=d["deduplicated"],
        detection_results=tuple(_deser_detection_result(x) for x in d["detection_results"]),
        incident_ids_touched=tuple(uuid.UUID(i) for i in d["incident_ids_touched"]),
        prev_hash=d["prev_hash"],
        this_hash=d["this_hash"],
        created_at=datetime.fromisoformat(d["created_at"]),
    )


def _ser_evidence(ev: IncidentEvidence) -> dict:
    return {"event_id": str(ev.event_id), "event_type": ev.event_type.value, "weight": ev.weight, "note": ev.note}


def _deser_evidence(d: dict) -> IncidentEvidence:
    return IncidentEvidence(event_id=uuid.UUID(d["event_id"]), event_type=SecurityEventType(d["event_type"]), weight=d["weight"], note=d["note"])


def _ser_incident(i: SecurityIncident) -> dict:
    return {
        "incident_id": str(i.incident_id),
        "threat_class": i.threat_class.value,
        "state": i.state.value,
        "severity": i.severity.value,
        "confidence": i.confidence.value,
        "owner_id": str(i.owner_id),
        "device_id": i.device_id,
        "evidence": [_ser_evidence(e) for e in i.evidence],
        "counter_evidence": [_ser_evidence(e) for e in i.counter_evidence],
        "rationale": i.rationale,
        "clearing_conditions": i.clearing_conditions,
        "opened_at": i.opened_at.isoformat(),
        "updated_at": i.updated_at.isoformat(),
        "source_pattern_or_rule_id": i.source_pattern_or_rule_id,
        "false_positive_reason": i.false_positive_reason,
    }


def _deser_incident(d: dict) -> SecurityIncident:
    return SecurityIncident(
        incident_id=uuid.UUID(d["incident_id"]),
        threat_class=ThreatClass(d["threat_class"]),
        state=IncidentState(d["state"]),
        severity=SecuritySeverity(d["severity"]),
        confidence=SecurityConfidence(d["confidence"]),
        owner_id=uuid.UUID(d["owner_id"]),
        device_id=d["device_id"],
        evidence=[_deser_evidence(e) for e in d["evidence"]],
        counter_evidence=[_deser_evidence(e) for e in d["counter_evidence"]],
        rationale=d["rationale"],
        clearing_conditions=d["clearing_conditions"],
        opened_at=datetime.fromisoformat(d["opened_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        source_pattern_or_rule_id=d["source_pattern_or_rule_id"],
        false_positive_reason=d["false_positive_reason"],
    )


def _ser_rule(r: DetectionRule) -> dict:
    return {
        "rule_id": r.rule_id,
        "version": r.version,
        "event_types": [t.value for t in r.event_types],
        "conditions": {
            k: (v.value if isinstance(v, SecuritySeverity) else v) for k, v in r.conditions.items()
        },
        "severity": r.severity.value,
        "confidence": r.confidence.value,
        "threat_class": r.threat_class.value,
        "required_evidence": list(r.required_evidence),
        "counter_evidence": list(r.counter_evidence),
        "recommended_action": r.recommended_action.value if r.recommended_action else None,
        "source": r.source,
        "provenance": r.provenance,
        "created_at": r.created_at.isoformat(),
        "verified_at": r.verified_at.isoformat() if r.verified_at else None,
        "enabled": r.enabled,
        "state": r.state.value,
        "rollback_version": r.rollback_version,
    }


def _deser_rule(d: dict) -> DetectionRule:
    from app.sentinel.types import DefensiveAction

    conditions = dict(d["conditions"])
    if "min_event_severity" in conditions and isinstance(conditions["min_event_severity"], str):
        conditions["min_event_severity"] = SecuritySeverity(conditions["min_event_severity"])
    return DetectionRule(
        rule_id=d["rule_id"],
        version=d["version"],
        event_types=frozenset(SecurityEventType(t) for t in d["event_types"]),
        conditions=conditions,
        severity=SecuritySeverity(d["severity"]),
        confidence=SecurityConfidence(d["confidence"]),
        threat_class=ThreatClass(d["threat_class"]),
        required_evidence=tuple(d["required_evidence"]),
        counter_evidence=tuple(d["counter_evidence"]),
        recommended_action=DefensiveAction(d["recommended_action"]) if d["recommended_action"] else None,
        source=d["source"],
        provenance=d["provenance"],
        created_at=datetime.fromisoformat(d["created_at"]),
        verified_at=datetime.fromisoformat(d["verified_at"]) if d["verified_at"] else None,
        enabled=d["enabled"],
        state=RuleState(d["state"]),
        rollback_version=d["rollback_version"],
    )


def _ser_canary(c: CanaryResource) -> dict:
    return {
        "canary_id": str(c.canary_id),
        "owner_id": str(c.owner_id),
        "kind": c.kind,
        "subject_ref": c.subject_ref,
        "created_at": c.created_at.isoformat(),
    }


def _deser_canary(d: dict) -> CanaryResource:
    return CanaryResource(
        canary_id=uuid.UUID(d["canary_id"]),
        owner_id=uuid.UUID(d["owner_id"]),
        kind=d["kind"],
        subject_ref=d["subject_ref"],
        created_at=datetime.fromisoformat(d["created_at"]),
    )


def _ser_tuning_candidate(c: RuleTuningCandidate) -> dict:
    return {
        "candidate_id": str(c.candidate_id),
        "rule_or_pattern_id": c.rule_or_pattern_id,
        "subject_ref": c.subject_ref,
        "incident_id": str(c.incident_id),
        "reason": c.reason,
        "created_at": c.created_at.isoformat(),
    }


def _deser_tuning_candidate(d: dict) -> RuleTuningCandidate:
    return RuleTuningCandidate(
        candidate_id=uuid.UUID(d["candidate_id"]),
        rule_or_pattern_id=d["rule_or_pattern_id"],
        subject_ref=d["subject_ref"],
        incident_id=uuid.UUID(d["incident_id"]),
        reason=d["reason"],
        created_at=datetime.fromisoformat(d["created_at"]),
    )


def to_snapshot(state: SentinelState) -> dict:
    """JSON-safe snapshot. Custom correlation_patterns beyond DEFAULT_CORRELATION_PATTERNS
    are NOT serialized (a documented limitation of this foundation prototype, mirrored after
    Guardian's explicit, non-pickle snapshot contract) -- from_snapshot() always restores the
    default pattern set."""
    return {
        "retention_policy": {
            "max_event_age_seconds": state.retention_policy.max_event_age_seconds,
            "max_events_retained": state.retention_policy.max_events_retained,
        },
        "allow_cross_device_correlation": state.allow_cross_device_correlation,
        "dedup_window_seconds": state.dedup_window_seconds,
        "events": [_ser_event(e) for e in state._events],
        "dedup_index": {k: v.isoformat() for k, v in state._dedup_index.items()},
        "incidents": {"|".join(k): _ser_incident(v) for k, v in state._incidents.items()},
        "receipts": [_ser_receipt(r) for r in state._receipts],
        "canaries": {k: _ser_canary(v) for k, v in state._canaries.items()},
        "tuning_candidates": [_ser_tuning_candidate(c) for c in state._tuning_candidates],
        "rules_current": {rid: _ser_rule(r) for rid, r in state.rule_registry._current.items()},
        "rules_history": {
            rid: {str(v): _ser_rule(r) for v, r in versions.items()} for rid, versions in state.rule_registry._history.items()
        },
    }


def from_snapshot(snapshot: dict) -> SentinelState:
    """Reconstructs a SentinelState from to_snapshot()'s output into a NEW object -- the
    round-trip test mutates state, serializes, deserializes into a fresh SentinelState, and
    asserts equality field-by-field."""
    state = SentinelState(
        retention_policy=RetentionPolicy(**snapshot["retention_policy"]),
        allow_cross_device_correlation=snapshot["allow_cross_device_correlation"],
        dedup_window_seconds=snapshot["dedup_window_seconds"],
    )
    state._events = [_deser_event(e) for e in snapshot["events"]]
    state._dedup_index = {k: datetime.fromisoformat(v) for k, v in snapshot["dedup_index"].items()}
    for key_str, incident_dict in snapshot["incidents"].items():
        owner_str, device_id, source_id = key_str.split("|", 2)
        state._incidents[(owner_str, device_id, source_id)] = _deser_incident(incident_dict)
    state._receipts = [_deser_receipt(r) for r in snapshot["receipts"]]
    state._canaries = {k: _deser_canary(v) for k, v in snapshot["canaries"].items()}
    state._tuning_candidates = [_deser_tuning_candidate(c) for c in snapshot["tuning_candidates"]]
    for rid, rule_dict in snapshot["rules_current"].items():
        state.rule_registry._current[rid] = _deser_rule(rule_dict)
    for rid, versions in snapshot["rules_history"].items():
        state.rule_registry._history[rid] = {int(v): _deser_rule(r) for v, r in versions.items()}
    return state
