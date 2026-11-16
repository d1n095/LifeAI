"""Sentinel -- detection rule lifecycle and matching (Stage V2-D1/D3).

NEW RULE != TRUSTED RULE: a rule only contributes trusted signal to correlation once it has
been explicitly promoted to VERIFIED or ACTIVE (see types.TRUSTED_RULE_STATES). This module
enforces that at the query level in evaluate_event_against_registry() -- a PROPOSED or
TESTING rule is never even considered, not merely filtered out after matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.sentinel.types import (
    TRUSTED_RULE_STATES,
    _VALID_RULE_TRANSITIONS,
    DetectionResult,
    DetectionRule,
    RuleState,
    SecurityEvent,
    SecuritySeverity,
    SentinelRuleError,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RuleRegistry:
    """Current rule per rule_id, plus a full version history for rollback. Mutated only via
    this module's functions -- no caller mutates `_current`/`_history` directly."""

    _current: dict[str, DetectionRule] = field(default_factory=dict)
    _history: dict[str, dict[int, DetectionRule]] = field(default_factory=dict)

    def current_rule(self, rule_id: str) -> DetectionRule | None:
        return self._current.get(rule_id)

    def all_current_rules(self) -> tuple[DetectionRule, ...]:
        return tuple(self._current.values())


def propose_rule(registry: RuleRegistry, rule: DetectionRule) -> DetectionRule:
    """Register a brand-new rule_id. Must start life in PROPOSED -- there is no shortcut to
    register a rule as already-trusted."""
    if rule.rule_id in registry._current:
        raise SentinelRuleError(f"rule_id {rule.rule_id!r} already exists; use promote/rollback, not propose")
    if rule.state != RuleState.PROPOSED:
        raise SentinelRuleError(f"a newly proposed rule must start in PROPOSED, got {rule.state}")
    registry._current[rule.rule_id] = rule
    registry._history.setdefault(rule.rule_id, {})[rule.version] = rule
    return rule


def promote_rule(registry: RuleRegistry, rule_id: str, *, to_state: RuleState, verified_at: datetime | None = None) -> DetectionRule:
    """Advance a rule along its lifecycle. Raises SentinelRuleError on any transition not
    explicitly listed in types._VALID_RULE_TRANSITIONS -- there is no default-allow path."""
    current = registry._current.get(rule_id)
    if current is None:
        raise SentinelRuleError(f"unknown rule_id {rule_id!r}")
    allowed = _VALID_RULE_TRANSITIONS.get(current.state, frozenset())
    if to_state not in allowed:
        raise SentinelRuleError(f"invalid rule transition {current.state} -> {to_state} for rule {rule_id!r}")
    updated = DetectionRule(
        rule_id=current.rule_id,
        version=current.version,
        event_types=current.event_types,
        conditions=current.conditions,
        severity=current.severity,
        confidence=current.confidence,
        threat_class=current.threat_class,
        required_evidence=current.required_evidence,
        counter_evidence=current.counter_evidence,
        recommended_action=current.recommended_action,
        source=current.source,
        provenance=current.provenance,
        created_at=current.created_at,
        verified_at=verified_at if to_state == RuleState.VERIFIED else current.verified_at,
        enabled=to_state in (RuleState.VERIFIED, RuleState.ACTIVE),
        state=to_state,
        rollback_version=current.rollback_version,
    )
    registry._current[rule_id] = updated
    registry._history[rule_id][updated.version] = updated
    return updated


def revoke_rule(registry: RuleRegistry, rule_id: str) -> DetectionRule:
    return promote_rule(registry, rule_id, to_state=RuleState.REVOKED)


def publish_new_version(registry: RuleRegistry, rule_id: str, new_rule: DetectionRule) -> DetectionRule:
    """Publish a new version of an existing rule (e.g. tightened conditions after a false
    positive). The new version always starts PROPOSED, regardless of the prior version's
    trust level -- version N+1 trust is earned independently of version N's (NEW RULE !=
    TRUSTED RULE applies per-version, not just per-rule_id)."""
    current = registry._current.get(rule_id)
    if current is None:
        raise SentinelRuleError(f"unknown rule_id {rule_id!r}; use propose_rule for a new rule_id")
    if new_rule.rule_id != rule_id or new_rule.version <= current.version:
        raise SentinelRuleError("new version must share rule_id and have version > current.version")
    if new_rule.state != RuleState.PROPOSED:
        raise SentinelRuleError("a newly published version must start in PROPOSED")
    if new_rule.rollback_version is None:
        new_rule = DetectionRule(
            rule_id=new_rule.rule_id,
            version=new_rule.version,
            event_types=new_rule.event_types,
            conditions=new_rule.conditions,
            severity=new_rule.severity,
            confidence=new_rule.confidence,
            threat_class=new_rule.threat_class,
            required_evidence=new_rule.required_evidence,
            counter_evidence=new_rule.counter_evidence,
            recommended_action=new_rule.recommended_action,
            source=new_rule.source,
            provenance=new_rule.provenance,
            created_at=new_rule.created_at,
            verified_at=None,
            enabled=False,
            state=RuleState.PROPOSED,
            rollback_version=current.version,
        )
    registry._current[rule_id] = new_rule
    registry._history[rule_id][new_rule.version] = new_rule
    return new_rule


def rollback_rule(registry: RuleRegistry, rule_id: str) -> DetectionRule:
    """Restore the version named by the CURRENT rule's rollback_version as the new current
    version, re-activated as ACTIVE (a rollback is meant to immediately restore working
    protection, not sit in PROPOSED again). Raises if there is no rollback target recorded."""
    current = registry._current.get(rule_id)
    if current is None:
        raise SentinelRuleError(f"unknown rule_id {rule_id!r}")
    if current.rollback_version is None:
        raise SentinelRuleError(f"rule {rule_id!r} has no rollback_version recorded")
    history = registry._history.get(rule_id, {})
    target = history.get(current.rollback_version)
    if target is None:
        raise SentinelRuleError(f"rollback target version {current.rollback_version} not found in history")
    restored = DetectionRule(
        rule_id=target.rule_id,
        version=current.version + 1,
        event_types=target.event_types,
        conditions=target.conditions,
        severity=target.severity,
        confidence=target.confidence,
        threat_class=target.threat_class,
        required_evidence=target.required_evidence,
        counter_evidence=target.counter_evidence,
        recommended_action=target.recommended_action,
        source=target.source,
        provenance=f"rollback-of-v{current.version}:{target.provenance}",
        created_at=target.created_at,
        verified_at=_utcnow(),
        enabled=True,
        state=RuleState.ACTIVE,
        rollback_version=target.rollback_version,
    )
    registry._current[rule_id] = restored
    registry._history[rule_id][restored.version] = restored
    return restored


# --- Matching. --------------------------------------------------------------------------


def rule_matches_event(rule: DetectionRule, event: SecurityEvent) -> bool:
    """Content-based match only -- trust/state is checked separately by the caller. Every
    condition key is checked with exact equality, never substring containment (the
    "substring match instead of exact match" bug shape found repeatedly elsewhere in this
    codebase does not apply here)."""
    if event.event_type not in rule.event_types:
        return False
    required_details: dict = rule.conditions.get("required_details", {})
    for key, expected in required_details.items():
        if event.details.get(key) != expected:
            return False
    min_severity = rule.conditions.get("min_event_severity")
    if min_severity is not None:
        from app.sentinel.types import SEVERITY_ORDER

        threshold = min_severity if isinstance(min_severity, SecuritySeverity) else SecuritySeverity(min_severity)
        if SEVERITY_ORDER[event.severity] < SEVERITY_ORDER[threshold]:
            return False
    return True


def evaluate_event_against_registry(registry: RuleRegistry, event: SecurityEvent) -> list[DetectionResult]:
    """Only rules whose CURRENT state is in TRUSTED_RULE_STATES are evaluated at all -- a
    PROPOSED/TESTING/DEPRECATED/REVOKED rule never produces a DetectionResult, trusted or
    not (fail-closed by construction, not by a downstream filter)."""
    results: list[DetectionResult] = []
    for rule in registry.all_current_rules():
        if rule.state not in TRUSTED_RULE_STATES:
            continue
        if not rule.enabled:
            continue
        matched = rule_matches_event(rule, event)
        if not matched:
            continue
        results.append(
            DetectionResult(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                event_id=event.event_id,
                matched=True,
                rule_state_at_evaluation=rule.state,
                threat_class=rule.threat_class,
                severity=rule.severity,
                confidence=rule.confidence,
            )
        )
    return results


def new_detection_rule(
    *,
    rule_id: str,
    event_types: frozenset,
    severity: SecuritySeverity,
    confidence,
    threat_class,
    conditions: dict | None = None,
    required_evidence: tuple[str, ...] = (),
    counter_evidence: tuple[str, ...] = (),
    recommended_action=None,
    source: str = "sentinel_default_pack",
    provenance: str = "builtin",
    version: int = 1,
    state: RuleState = RuleState.PROPOSED,
) -> DetectionRule:
    """Convenience constructor -- still requires an explicit `state` (default PROPOSED, so a
    caller cannot accidentally register a rule as pre-trusted by omission)."""
    return DetectionRule(
        rule_id=rule_id,
        version=version,
        event_types=event_types,
        conditions=conditions or {},
        severity=severity,
        confidence=confidence,
        threat_class=threat_class,
        required_evidence=required_evidence,
        counter_evidence=counter_evidence,
        recommended_action=recommended_action,
        source=source,
        provenance=provenance,
        created_at=_utcnow(),
        verified_at=None,
        enabled=False,
        state=state,
        rollback_version=None,
    )
