from app.mainai_execution.supervision_diagnostics import (
    AnomalyLevel,
    DecisionJournal,
    DecisionJournalEntry,
    EvidenceLedger,
    Escalation,
    HysteresisGate,
    SupervisionMetrics,
    choose_escalation,
    classify_anomaly,
    run_fault_injection_soak,
    run_crash_matrix,
)


def test_evidence_is_deduplicated_and_expires():
    ledger = EvidenceLedger()
    first = ledger.add(source="tests", payload_fingerprint="sha-a", observed_at=10, valid_for=5, classification="PASS")
    second = ledger.add(source="tests", payload_fingerprint="sha-a", observed_at=11, valid_for=5, classification="PASS")
    assert first.evidence_id == second.evidence_id and len(ledger) == 1
    assert ledger.is_current(first.evidence_id, 14)
    assert not ledger.is_current(first.evidence_id, 16)


def test_hysteresis_and_decision_journal_fence_transient_retries():
    gate = HysteresisGate(cooldown_seconds=10)
    assert gate.allow("provider-a", 0)
    assert not gate.allow("provider-a", 5)
    assert gate.allow("provider-a", 10)
    journal = DecisionJournal()
    journal.append(DecisionJournalEntry("FAILOVER", ("usage_exhausted",), True, ("provider-b",), "portable", "new lease", "quarantine"))
    assert len(journal.entries) == 1


def test_anomaly_escalation_metrics_and_large_fault_soak():
    assert classify_anomaly(failure_rate=0, continuation_rate=0, dead_letter_rate=0, queue_latency_seconds=1) is AnomalyLevel.NORMAL
    assert classify_anomaly(failure_rate=0.7, continuation_rate=0, dead_letter_rate=0, queue_latency_seconds=1) is AnomalyLevel.DEGRADED
    assert choose_escalation(repeated_failures=0, authorized_providers=0, fix_attempts=0, max_fix_attempts=3, owner_authority=True) is Escalation.QUARANTINE
    metrics = SupervisionMetrics()
    metrics.inc("continuations", 10)
    assert metrics.counters["continuations"] == 1
    result = run_fault_injection_soak(1000)
    assert result["jobs"] == 1000 and result["events"] >= 3000
    assert result["assignments"] == 1000
    crash = run_crash_matrix()
    assert crash["boundaries"] >= 20 and crash["stale_replays"] == 0
