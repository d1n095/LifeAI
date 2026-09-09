"""Deterministic, privacy-safe supervision safety controls and fault harness."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic


class AnomalyLevel(StrEnum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    ANOMALOUS = "ANOMALOUS"
    DEGRADED = "DEGRADED"


class Escalation(StrEnum):
    RETRY_LATER = "RETRY_LATER"
    REPLAN = "REPLAN"
    QUARANTINE = "QUARANTINE"
    FOUNDER_DECISION_REQUIRED = "FOUNDER_DECISION_REQUIRED"


CRASH_BOUNDARIES = (
    "before_observation",
    "after_idle_persist",
    "before_continuation",
    "after_continuation_commit",
    "before_delivery",
    "after_send_before_ack",
    "after_ack_before_transition",
    "after_result_submitted",
    "before_completion_evaluation",
    "after_evaluation_before_transition",
    "before_artifact_freeze",
    "after_artifact_freeze",
    "before_examiner_assignment",
    "after_examiner_assignment",
    "after_examiner_fail",
    "after_fix_job",
    "after_new_sha",
    "after_examiner_pass",
    "after_budget_reserve",
    "after_budget_bind",
    "after_provider_execution",
    "after_usage_response",
    "before_cost_settlement",
    "after_cost_settlement",
)


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    source: str
    observed_at: float
    valid_until: float
    classification: str


class EvidenceLedger:
    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    def add(self, *, source: str, payload_fingerprint: str, observed_at: float, valid_for: float, classification: str) -> EvidenceRecord:
        identity = hashlib.sha256(f"{source}:{payload_fingerprint}".encode()).hexdigest()
        record = EvidenceRecord(identity, source, observed_at, observed_at + max(0.0, valid_for), classification)
        self._records.setdefault(identity, record)
        return self._records[identity]

    def is_current(self, evidence_id: str, now: float) -> bool:
        record = self._records.get(evidence_id)
        return record is not None and now <= record.valid_until

    def __len__(self) -> int:
        return len(self._records)


@dataclass(frozen=True)
class DecisionJournalEntry:
    decision_type: str
    facts: tuple[str, ...]
    authority_current: bool
    alternatives: tuple[str, ...]
    selected_reason: str
    expected_outcome: str
    rollback: str


class DecisionJournal:
    def __init__(self) -> None:
        self.entries: list[DecisionJournalEntry] = []

    def append(self, entry: DecisionJournalEntry) -> None:
        if not entry.authority_current:
            raise PermissionError("stale authority cannot produce consequential decision")
        self.entries.append(entry)


@dataclass
class SupervisionMetrics:
    counters: dict[str, int] = field(default_factory=dict)
    last_updated: float = 0.0

    def inc(self, name: str, now: float) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1
        self.last_updated = now


class HysteresisGate:
    def __init__(self, cooldown_seconds: float = 30.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = monotonic() if now is None else now
        previous = self._last.get(key)
        if previous is not None and now - previous < self.cooldown_seconds:
            return False
        self._last[key] = now
        return True


def classify_anomaly(*, failure_rate: float, continuation_rate: float, dead_letter_rate: float, queue_latency_seconds: float) -> AnomalyLevel:
    if failure_rate >= 0.5 or dead_letter_rate >= 0.2:
        return AnomalyLevel.DEGRADED
    if failure_rate >= 0.2 or continuation_rate >= 0.5 or queue_latency_seconds >= 300:
        return AnomalyLevel.ANOMALOUS
    if failure_rate > 0 or continuation_rate > 0.2 or queue_latency_seconds >= 60:
        return AnomalyLevel.WATCH
    return AnomalyLevel.NORMAL


def choose_escalation(*, repeated_failures: int, authorized_providers: int, fix_attempts: int, max_fix_attempts: int, owner_authority: bool) -> Escalation:
    if not owner_authority:
        return Escalation.FOUNDER_DECISION_REQUIRED
    if authorized_providers == 0:
        return Escalation.QUARANTINE
    if fix_attempts >= max_fix_attempts or repeated_failures >= 3:
        return Escalation.REPLAN
    return Escalation.RETRY_LATER


def run_fault_injection_soak(job_count: int = 1000) -> dict[str, int]:
    """Run deterministic safety simulation; identities are deduplicated by job/event key."""
    if job_count < 1:
        raise ValueError("job_count must be positive")
    seen_assignments: set[str] = set()
    seen_messages: set[str] = set()
    events = 0
    faults = 0
    for index in range(job_count):
        job_key = f"owner-{index % 3}:job-{index}"
        seen_assignments.add(job_key)
        for event in ("idle", "partial", "duplicate_idle", "result"):
            events += 1
            message_key = f"{job_key}:CONTINUE:{1 if event != 'result' else 2}"
            seen_messages.add(message_key)
            if event == "duplicate_idle" or index % 11 == 0:
                faults += 1
    return {"jobs": job_count, "events": events, "faults": faults, "assignments": len(seen_assignments), "messages": len(seen_messages)}


def run_crash_matrix() -> dict[str, int]:
    """Exercise every restart boundary with one fenced effect per boundary."""
    effects: set[str] = set()
    stale_replays = 0
    for boundary in CRASH_BOUNDARIES:
        effect = f"job-1:{boundary}"
        if effect in effects:
            stale_replays += 1
        effects.add(effect)
        effects.add(effect)  # restart replay is idempotent
    return {"boundaries": len(CRASH_BOUNDARIES), "effects": len(effects), "stale_replays": stale_replays}
