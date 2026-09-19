"""Sentinel -- security knowledge pack (threat pack) loading (Stage V2-D1).

Loading/validating a SecurityKnowledgePack never performs a network call and never reads
from any path implicitly -- `load_threat_pack()` only ever operates on a
SecurityKnowledgePack object already constructed in memory by the caller. This keeps the
whole package usable fully offline, matching the founder's constitution requirement.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.sentinel.types import (
    DefensiveAction,
    DetectionRule,
    RuleState,
    SecurityConfidence,
    SecurityEventType,
    SecurityKnowledgePack,
    SecuritySeverity,
    SentinelRuleError,
    ThreatClass,
    ThreatPackTestVector,
)


def load_threat_pack(pack: SecurityKnowledgePack) -> SecurityKnowledgePack:
    """Validates structural invariants and returns the pack unchanged (no network access,
    no filesystem access -- purely a validation pass over an already-in-memory object)."""
    if not pack.pack_id or not pack.pack_version:
        raise SentinelRuleError("threat pack must have a pack_id and pack_version")
    if pack.valid_until is not None and pack.valid_until < pack.valid_from:
        raise SentinelRuleError("threat pack valid_until must not precede valid_from")
    rule_ids = {r.rule_id for r in pack.detection_rules}
    for mapped_rule_id in pack.mitre_attack_mappings:
        if mapped_rule_id not in rule_ids:
            raise SentinelRuleError(f"mitre_attack_mappings references unknown rule_id {mapped_rule_id!r}")
    for vector in pack.test_vectors:
        if vector.expected_rule_id not in rule_ids:
            raise SentinelRuleError(f"test vector references unknown rule_id {vector.expected_rule_id!r}")
    return pack


def _synthetic_rule() -> DetectionRule:
    return DetectionRule(
        rule_id="test.pack.unsigned_binary.v1",
        version=1,
        event_types=frozenset({SecurityEventType.UNSIGNED_BINARY_EXECUTED}),
        conditions={"min_event_severity": SecuritySeverity.HIGH},
        severity=SecuritySeverity.HIGH,
        confidence=SecurityConfidence.MEDIUM,
        threat_class=ThreatClass.RECONNAISSANCE,
        required_evidence=("binary_signer_unknown",),
        counter_evidence=("binary_signer_known_good",),
        recommended_action=DefensiveAction.FREEZE_PROCESS,
        source="example_threat_pack",
        provenance="synthetic test fixture, not real threat intelligence",
        created_at=datetime.now(timezone.utc),
        verified_at=None,
        enabled=False,
        state=RuleState.PROPOSED,
        rollback_version=None,
    )


EXAMPLE_TEST_PACK = SecurityKnowledgePack(
    pack_id="example-test-pack",
    pack_version="0.0.1-test",
    source_provenance="synthetic test fixture -- not real threat intelligence, for tests only",
    valid_from=date(2026, 1, 1),
    valid_until=None,
    detection_rules=(_synthetic_rule(),),
    malware_families=("example_family_a",),
    behavior_patterns=("mass_read_then_egress",),
    known_bad_hashes=frozenset({"deadbeef" * 8}),
    known_bad_domain_classes=frozenset({"newly_registered_domain"}),
    exploit_patterns=("example_exploit_pattern",),
    mitre_attack_mappings={"test.pack.unsigned_binary.v1": ("T1204", "T1055")},
    false_positive_exceptions=("known_dev_tool_signer",),
    test_vectors=(
        ThreatPackTestVector(
            description="unsigned binary executed at HIGH severity should match the synthetic rule",
            event_types=(SecurityEventType.UNSIGNED_BINARY_EXECUTED,),
            expected_rule_id="test.pack.unsigned_binary.v1",
        ),
    ),
)
