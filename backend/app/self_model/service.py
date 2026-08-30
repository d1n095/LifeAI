"""Stage E — MainAI self-model / capability ledger (evidence projection)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capability_reality import get_capability_reality, list_capability_records, record_capability_observation
from app.models.capability_reality import CapabilityObservationEvent, CapabilityRecord


@dataclass
class CapabilityLedgerEntry:
    capability_key: str
    domain: str
    status: str
    confidence: float | None
    last_proof_at: datetime | None
    last_proof_evidence_id: uuid.UUID | None
    success_count: int
    failure_count: int
    failure_patterns: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    regression_history: list[dict] = field(default_factory=list)
    founder_interventions: int = 0
    next_improvement_candidate: str | None = None
    weak: bool = False
    evidence_only: bool = True
    # Explicit: model/numeric confidence alone is NOT proof.
    confidence_is_not_evidence: bool = True


@dataclass
class SelfModelSnapshot:
    owner_id: uuid.UUID
    generated_at: datetime
    entries: list[CapabilityLedgerEntry] = field(default_factory=list)
    proven: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    repeatedly_failing: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)
    improved: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)


def _events_for(db: Session, *, owner_id: uuid.UUID, record_id: uuid.UUID) -> list[CapabilityObservationEvent]:
    return list(
        db.execute(
            select(CapabilityObservationEvent)
            .where(
                CapabilityObservationEvent.owner_id == owner_id,
                CapabilityObservationEvent.capability_record_id == record_id,
            )
            .order_by(CapabilityObservationEvent.created_at)
        ).scalars().all()
    )


def build_ledger_entry(db: Session, *, owner_id: uuid.UUID, record: CapabilityRecord) -> CapabilityLedgerEntry:
    events = _events_for(db, owner_id=owner_id, record_id=record.id)
    success_count = sum(1 for e in events if e.event_type == "success_recorded" or (e.detail or {}).get("success") is True)
    failure_count = sum(1 for e in events if e.event_type == "failure_recorded" or (e.detail or {}).get("success") is False)
    failure_patterns: list[str] = []
    corrections: list[str] = []
    regressions: list[dict] = []
    founder_interventions = 0
    prev_status: str | None = None
    for event in events:
        detail = event.detail or {}
        reason = detail.get("status_reason") or detail.get("reason")
        if event.event_type == "failure_recorded" and reason:
            failure_patterns.append(str(reason))
        if event.event_type == "gap_recorded" and reason:
            failure_patterns.append(f"gap:{reason}")
        if detail.get("authority") == "founder":
            founder_interventions += 1
            if reason:
                corrections.append(str(reason))
        status = detail.get("status")
        if (
            prev_status == "verified_available"
            and status
            and status != "verified_available"
            and event.event_type == "status_changed"
        ):
            regressions.append(
                {
                    "at": event.created_at.isoformat(),
                    "from": prev_status,
                    "to": status,
                    "event_id": str(event.id),
                }
            )
        if status:
            prev_status = status

    # Deduplicate patterns while preserving order.
    seen: set[str] = set()
    uniq_patterns: list[str] = []
    for p in failure_patterns:
        if p not in seen:
            seen.add(p)
            uniq_patterns.append(p)

    weak = record.status != "verified_available" or failure_count > success_count or bool(regressions)
    next_improvement = None
    if uniq_patterns:
        next_improvement = f"address:{uniq_patterns[-1][:120]}"
    elif record.status == "planned":
        next_improvement = "prove_with_real_success_observation"
    elif record.status == "verified_available" and failure_count:
        next_improvement = "investigate_intermittent_failures"

    return CapabilityLedgerEntry(
        capability_key=record.capability_key,
        domain=record.domain,
        status=record.status,
        confidence=float(record.confidence) if record.confidence is not None else None,
        last_proof_at=record.last_verified_at or record.last_success_at,
        last_proof_evidence_id=record.last_verification_evidence_id,
        success_count=success_count,
        failure_count=failure_count,
        failure_patterns=uniq_patterns,
        corrections=corrections,
        regression_history=regressions,
        founder_interventions=founder_interventions,
        next_improvement_candidate=next_improvement,
        weak=weak,
    )


def build_self_model(db: Session, *, owner_id: uuid.UUID, domain: str | None = None) -> SelfModelSnapshot:
    """Project MainAI's evidence-backed self-model from durable capability observations.

    Confidence alone is never treated as proof — only verification evidence / success events.
    """
    records = list_capability_records(db, owner_id=owner_id, domain=domain)
    entries = [build_ledger_entry(db, owner_id=owner_id, record=r) for r in records]
    proven = [e.capability_key for e in entries if e.status == "verified_available" and e.last_proof_evidence_id is not None]
    failed = [e.capability_key for e in entries if e.failure_count > 0]
    repeatedly_failing = [e.capability_key for e in entries if e.failure_count >= 2]
    weak = [e.capability_key for e in entries if e.weak]
    regressed = [e.capability_key for e in entries if e.regression_history]
    improved = [
        e.capability_key
        for e in entries
        if e.status == "verified_available" and e.success_count > 0 and not e.regression_history
    ]
    return SelfModelSnapshot(
        owner_id=owner_id,
        generated_at=datetime.utcnow(),
        entries=entries,
        proven=proven,
        failed=failed,
        repeatedly_failing=repeatedly_failing,
        weak=weak,
        improved=improved,
        regressed=regressed,
    )


def record_proven_capability(
    db: Session,
    *,
    owner_id: uuid.UUID,
    capability_key: str,
    domain: str,
    verification_evidence_id: uuid.UUID,
    authority: str = "deterministic_source",
    status_reason: str | None = None,
) -> CapabilityRecord:
    """Caller observed a REAL success backed by durable same-owner evidence.

    PROVEN requires a valid intelligence_evidence FK — never confidence or free text alone.
    """
    return record_capability_observation(
        db,
        owner_id=owner_id,
        capability_key=capability_key,
        domain=domain,
        status="verified_available",
        status_reason=status_reason or "real_success_observed",
        authority=authority,
        verification_evidence_id=verification_evidence_id,
        success=True,
        provenance={"stage": "E", "ledger": "self_model"},
    )


def record_failed_capability(
    db: Session,
    *,
    owner_id: uuid.UUID,
    capability_key: str,
    domain: str,
    reason: str,
    authority: str = "deterministic_source",
    demote_from_verified: bool = True,
) -> CapabilityRecord:
    existing = get_capability_reality(db, owner_id=owner_id, capability_key=capability_key)
    status = "configured_unavailable"
    if existing and existing.status == "verified_available" and demote_from_verified:
        status = "configured_unavailable"
    elif existing:
        status = existing.status if existing.status != "verified_available" else "configured_unavailable"
    else:
        status = "unknown"
    return record_capability_observation(
        db,
        owner_id=owner_id,
        capability_key=capability_key,
        domain=domain,
        status=status,
        status_reason=reason,
        authority=authority,
        success=False,
        provenance={"stage": "E", "ledger": "self_model"},
    )


def record_founder_intervention(
    db: Session,
    *,
    owner_id: uuid.UUID,
    capability_key: str,
    domain: str,
    reason: str,
    resulting_status: str = "planned",
) -> CapabilityRecord:
    return record_capability_observation(
        db,
        owner_id=owner_id,
        capability_key=capability_key,
        domain=domain,
        status=resulting_status,
        status_reason=reason,
        authority="founder",
        provenance={"stage": "E", "founder_intervention": True},
    )
