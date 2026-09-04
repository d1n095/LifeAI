"""Sentinel Core -- types (MainAI V2, Stages V2-D1..D4).

Standalone, isolated, NOT imported by any production runtime path, and does NOT import
app.guardian or app.privacy_boundary (SENTINEL DETECTION != AUTHORITY: composing a
DefensiveActionRequest with Guardian's policy evaluation happens only in a future
cross-layer caller, never inside this package -- exactly like guardian/ and
privacy_boundary/ stay independent of each other). See
docs/mainai_v2/MAINAI_V2_SENTINEL_SECURITY.md for the design this implements.

Sentinel is NOT MainAI: it normalizes security observations, classifies severity,
correlates suspicious behavior, and proposes bounded DefensiveActionRequest objects -- it
never executes a privileged action itself. No function anywhere in this package returns or
constructs anything that grants authority, and DefensiveActionRequest has no execute()/
apply() method anywhere in the package (see test_sentinel_cannot_execute_guardian_action).

SPECIALIZATION != SURVEILLANCE: SecurityEvent.details is a structured dict of pre-classified
signal fields (hashes, categories, booleans, short classification strings) -- never a place
for raw document content, private conversation text, or free-form user data. That is
enforced structurally in app.sentinel.service.record_event() (see _validate_details()), not
just documented here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SecuritySeverity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


SEVERITY_ORDER: dict[SecuritySeverity, int] = {
    SecuritySeverity.INFO: 0,
    SecuritySeverity.LOW: 1,
    SecuritySeverity.MEDIUM: 2,
    SecuritySeverity.HIGH: 3,
    SecuritySeverity.CRITICAL: 4,
}


class SecurityConfidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


CONFIDENCE_ORDER: dict[SecurityConfidence, int] = {
    SecurityConfidence.LOW: 0,
    SecurityConfidence.MEDIUM: 1,
    SecurityConfidence.HIGH: 2,
}


class ThreatClass(str, Enum):
    EXFILTRATION = "EXFILTRATION"
    CREDENTIAL_THEFT = "CREDENTIAL_THEFT"
    RANSOMWARE = "RANSOMWARE"
    MODEL_TAMPERING = "MODEL_TAMPERING"
    PRIVILEGE_ESCALATION = "PRIVILEGE_ESCALATION"
    PERSISTENCE = "PERSISTENCE"
    RECONNAISSANCE = "RECONNAISSANCE"
    CANARY_INTERACTION = "CANARY_INTERACTION"
    DEVICE_TAMPERING = "DEVICE_TAMPERING"
    UNKNOWN = "UNKNOWN"


class SecurityEventType(str, Enum):
    PROCESS_STARTED = "PROCESS_STARTED"
    PROCESS_INJECTION_ATTEMPT = "PROCESS_INJECTION_ATTEMPT"
    UNSIGNED_BINARY_EXECUTED = "UNSIGNED_BINARY_EXECUTED"
    BINARY_CHANGED = "BINARY_CHANGED"
    MODEL_CHANGED = "MODEL_CHANGED"
    PLUGIN_CHANGED = "PLUGIN_CHANGED"
    MASS_FILE_READ = "MASS_FILE_READ"
    MASS_FILE_WRITE = "MASS_FILE_WRITE"
    RANSOMWARE_PATTERN = "RANSOMWARE_PATTERN"
    CREDENTIAL_READ_ATTEMPT = "CREDENTIAL_READ_ATTEMPT"
    VAULT_ACCESS_ATTEMPT = "VAULT_ACCESS_ATTEMPT"
    UNEXPECTED_EGRESS = "UNEXPECTED_EGRESS"
    NEW_OUTBOUND_DESTINATION = "NEW_OUTBOUND_DESTINATION"
    SUSPICIOUS_DNS = "SUSPICIOUS_DNS"
    USB_CONNECTED = "USB_CONNECTED"
    USB_HID_BEHAVIOR = "USB_HID_BEHAVIOR"
    BLUETOOTH_PAIR_ATTEMPT = "BLUETOOTH_PAIR_ATTEMPT"
    WIFI_NETWORK_CHANGE = "WIFI_NETWORK_CHANGE"
    BROWSER_EXPLOIT_SIGNAL = "BROWSER_EXPLOIT_SIGNAL"
    UNTRUSTED_FILE_OPENED = "UNTRUSTED_FILE_OPENED"
    SCRIPT_EXECUTION = "SCRIPT_EXECUTION"
    PRIVILEGE_ESCALATION_ATTEMPT = "PRIVILEGE_ESCALATION_ATTEMPT"
    AGENT_SCOPE_ESCALATION = "AGENT_SCOPE_ESCALATION"
    POLICY_CHANGE = "POLICY_CHANGE"
    SECURITY_SETTING_CHANGED = "SECURITY_SETTING_CHANGED"
    BOOT_INTEGRITY_FAILURE = "BOOT_INTEGRITY_FAILURE"
    DEVICE_TRUST_CHANGED = "DEVICE_TRUST_CHANGED"
    RECOVERY_TRIGGER = "RECOVERY_TRIGGER"
    CANARY_TOUCHED = "CANARY_TOUCHED"


class IncidentState(str, Enum):
    OBSERVED = "OBSERVED"
    SUSPECTED = "SUSPECTED"
    CONFIRMED = "CONFIRMED"
    CONTAINED = "CONTAINED"
    RECOVERING = "RECOVERING"
    RESOLVED = "RESOLVED"
    FALSE_POSITIVE = "FALSE_POSITIVE"


class RuleState(str, Enum):
    PROPOSED = "PROPOSED"
    TESTING = "TESTING"
    VERIFIED = "VERIFIED"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    REVOKED = "REVOKED"


# NEW RULE != TRUSTED RULE: only a rule in one of these states may contribute a
# DetectionResult that correlation treats as trustworthy signal. See rules.py's
# evaluate_event_against_registry(), which excludes every other state at the query level
# (not merely filtered after the fact).
TRUSTED_RULE_STATES = frozenset({RuleState.VERIFIED, RuleState.ACTIVE})

# Valid rule lifecycle transitions. Revocation is reachable from any non-terminal state
# (a bad rule must be revocable immediately, not only after walking back through every
# earlier stage) but never reversible -- there is no transition OUT of REVOKED.
_VALID_RULE_TRANSITIONS: dict[RuleState, frozenset[RuleState]] = {
    RuleState.PROPOSED: frozenset({RuleState.TESTING, RuleState.REVOKED}),
    RuleState.TESTING: frozenset({RuleState.VERIFIED, RuleState.REVOKED}),
    RuleState.VERIFIED: frozenset({RuleState.ACTIVE, RuleState.REVOKED}),
    RuleState.ACTIVE: frozenset({RuleState.DEPRECATED, RuleState.REVOKED}),
    RuleState.DEPRECATED: frozenset({RuleState.REVOKED}),
    RuleState.REVOKED: frozenset(),
}


class DefensiveAction(str, Enum):
    BLOCK_PROCESS_NETWORK = "BLOCK_PROCESS_NETWORK"
    QUARANTINE_FILE = "QUARANTINE_FILE"
    FREEZE_PROCESS = "FREEZE_PROCESS"
    REVOKE_AGENT_LEASE = "REVOKE_AGENT_LEASE"
    LOCK_VAULT = "LOCK_VAULT"
    DISABLE_PROVIDER_ACCESS = "DISABLE_PROVIDER_ACCESS"
    ISOLATE_BROWSER = "ISOLATE_BROWSER"
    DISABLE_NEW_DEVICE_ACCESS = "DISABLE_NEW_DEVICE_ACCESS"
    SWITCH_LOCAL_ONLY = "SWITCH_LOCAL_ONLY"
    ENTER_SAFE_MODE = "ENTER_SAFE_MODE"
    REQUEST_RECOVERY = "REQUEST_RECOVERY"


# --- Event mesh primitives. -------------------------------------------------------------


@dataclass(frozen=True)
class SecuritySubject:
    """WHO/WHAT this event is about -- never a raw content blob. owner_id/device_id scope
    the event for cross-owner/cross-device correlation gating (see correlation.py):
    cross-owner events are NEVER correlated, cross-device only when explicitly allowed."""

    owner_id: uuid.UUID
    device_id: str
    subject_kind: str  # "process" | "file" | "network_destination" | "device" | "model" | "plugin" | "vault_object" | "canary"
    subject_ref: str  # opaque identifier/hash/class -- never a raw path carrying private content


@dataclass(frozen=True)
class SecuritySource:
    adapter_name: str  # e.g. "file_scanner", "process_monitor", "canary"
    adapter_version: str
    collected_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class SecurityEvent:
    """A normalized security observation. See module docstring: `details` is validated by
    app.sentinel.service.record_event() to reject raw free-text content."""

    event_id: uuid.UUID
    event_type: SecurityEventType
    severity: SecuritySeverity
    confidence: SecurityConfidence
    subject: SecuritySubject
    source: SecuritySource
    occurred_at: datetime
    correlation_id: uuid.UUID
    parent_event_id: uuid.UUID | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventReceipt:
    """Immutable-by-convention, append-only, hash-chained -- same discipline as Guardian's
    ContainmentReceipt chain (see app.guardian.service._receipt_hash/_record_receipt)."""

    receipt_id: uuid.UUID
    event_id: uuid.UUID
    event_type: SecurityEventType
    deduplicated: bool
    detection_results: tuple["DetectionResult", ...]
    incident_ids_touched: tuple[uuid.UUID, ...]
    prev_hash: str
    this_hash: str = ""
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class RetentionPolicy:
    max_event_age_seconds: int
    max_events_retained: int


# --- Detection rules. ----------------------------------------------------------------


@dataclass(frozen=True)
class DetectionRule:
    rule_id: str
    version: int
    event_types: frozenset[SecurityEventType]
    conditions: dict[str, Any]
    severity: SecuritySeverity
    confidence: SecurityConfidence
    threat_class: ThreatClass
    required_evidence: tuple[str, ...]
    counter_evidence: tuple[str, ...]
    recommended_action: DefensiveAction | None
    source: str
    provenance: str
    created_at: datetime
    verified_at: datetime | None
    enabled: bool
    state: RuleState
    rollback_version: int | None


@dataclass(frozen=True)
class DetectionResult:
    """Produced by evaluating one DetectionRule against one SecurityEvent. Only results
    whose rule_state_at_evaluation is in TRUSTED_RULE_STATES are eligible to feed
    correlation -- see rules.py's evaluate_event_against_registry()."""

    rule_id: str
    rule_version: int
    event_id: uuid.UUID
    matched: bool
    rule_state_at_evaluation: RuleState
    threat_class: ThreatClass
    severity: SecuritySeverity
    confidence: SecurityConfidence


# --- Incident correlation. ------------------------------------------------------------


@dataclass(frozen=True)
class RequiredIndicator:
    """One indicator a CorrelationPattern needs satisfied by SOME event in the correlation
    window. `required_details` is a subset-match: every key/value listed must be present and
    equal on the candidate event's `details` (exact match, never substring -- the
    "substring match instead of exact match" bug shape found repeatedly elsewhere in this
    codebase does not apply here because dict equality on individual keys is always exact)."""

    event_type: SecurityEventType
    required_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CorrelationPattern:
    pattern_id: str
    threat_class: ThreatClass
    required_indicators: tuple[RequiredIndicator, ...]
    window_seconds: int
    severity: SecuritySeverity
    confidence_on_full_match: SecurityConfidence
    rationale: str
    clearing_conditions: str
    allow_cross_device: bool = False


@dataclass(frozen=True)
class IncidentEvidence:
    event_id: uuid.UUID
    event_type: SecurityEventType
    weight: float
    note: str


@dataclass
class SecurityIncident:
    """Mutable; owned/mutated only via app.sentinel.correlation's functions (mirrors
    GuardianState's "no public mutation outside the service module" discipline)."""

    incident_id: uuid.UUID
    threat_class: ThreatClass
    state: IncidentState
    severity: SecuritySeverity
    confidence: SecurityConfidence
    owner_id: uuid.UUID
    device_id: str
    evidence: list[IncidentEvidence]
    counter_evidence: list[IncidentEvidence]
    rationale: str
    clearing_conditions: str
    opened_at: datetime
    updated_at: datetime
    source_pattern_or_rule_id: str
    false_positive_reason: str | None = None


@dataclass(frozen=True)
class RuleTuningCandidate:
    """A false-positive closeout's only durable effect: a scoped, NOT-auto-applied proposal.
    FALSE POSITIVE CORRECTION != GLOBAL TRUST GRANT -- this record changes nothing by
    itself; a human/owner would review it and, separately, PROPOSED a new rule version."""

    candidate_id: uuid.UUID
    rule_or_pattern_id: str
    subject_ref: str
    incident_id: uuid.UUID
    reason: str
    created_at: datetime = field(default_factory=_utcnow)


# --- Defensive action requests. --------------------------------------------------------


@dataclass(frozen=True)
class ContainmentRecommendation:
    threat_class: ThreatClass
    recommended_action: DefensiveAction
    scope_hint: str
    rationale: str


@dataclass(frozen=True)
class PreauthorizedDefense:
    """A structurally-bounded standing permission -- never a general "act as needed" grant
    (DEFENSIVE AUTONOMY != GENERAL AUTONOMY). Every field is required and scalar; there is
    no wildcard "all actions"/"all scopes" value accepted anywhere in this dataclass or in
    require_bounded_preauthorization() (see defensive_action.py)."""

    allowed_action: DefensiveAction
    scope_hint: str
    min_severity: SecuritySeverity
    min_confidence: SecurityConfidence
    time_to_damage_class: str  # e.g. "seconds" | "minutes" | "hours"
    owner_response_timeout_seconds: int
    max_containment_duration_seconds: int
    rollback_conditions: str


@dataclass(frozen=True)
class DefensiveActionRequest:
    """Pure data -- deliberately has no execute()/apply() method anywhere in this package.
    Sentinel can construct this and ONLY this; turning it into an actual containment action
    requires a future caller to build a Guardian ContainmentRequest from it and get Guardian's
    own decision. That composition does not live here (see module docstring)."""

    request_id: uuid.UUID
    action: DefensiveAction
    scope_hint: str
    threat_class: ThreatClass
    severity: SecuritySeverity
    confidence: SecurityConfidence
    incident_id: uuid.UUID
    owner_id: uuid.UUID
    reason: str
    preauthorized_by: PreauthorizedDefense | None
    requested_at: datetime = field(default_factory=_utcnow)


# --- Canary / honeypot foundation. ------------------------------------------------------


@dataclass(frozen=True)
class CanaryResource:
    """`subject_ref` must be an explicitly synthetic fixture identifier -- never a
    real-looking credential value (see canary.py's register_canary() validation, which
    exists specifically to avoid repeating this session's earlier GitHub push-protection
    incident with a Stripe-shaped test string)."""

    canary_id: uuid.UUID
    owner_id: uuid.UUID
    kind: str  # "fake_secret" | "fake_credential_file" | "fake_vault_object" | "unused_document_path"
    subject_ref: str
    created_at: datetime = field(default_factory=_utcnow)


# --- Security knowledge pack (threat pack) format. ---------------------------------------


@dataclass(frozen=True)
class ThreatPackTestVector:
    description: str
    event_types: tuple[SecurityEventType, ...]
    expected_rule_id: str


@dataclass(frozen=True)
class SecurityKnowledgePack:
    """Offline threat-pack format. Loading/parsing this must never perform a network call
    (see threat_pack.py's load_threat_pack(), which only ever reads the in-memory/local
    dataclass passed to it)."""

    pack_id: str
    pack_version: str
    source_provenance: str
    valid_from: date
    valid_until: date | None
    detection_rules: tuple[DetectionRule, ...]
    malware_families: tuple[str, ...]
    behavior_patterns: tuple[str, ...]
    known_bad_hashes: frozenset[str]
    known_bad_domain_classes: frozenset[str]
    exploit_patterns: tuple[str, ...]
    mitre_attack_mappings: dict[str, tuple[str, ...]]  # rule_id -> ATT&CK technique ids
    false_positive_exceptions: tuple[str, ...]
    test_vectors: tuple[ThreatPackTestVector, ...]


class SentinelPrivacyViolation(ValueError):
    """Raised when a caller tries to attach raw/free-text content to a SecurityEvent."""


class SentinelRuleError(ValueError):
    """Raised on an invalid rule lifecycle transition or malformed rule definition."""
