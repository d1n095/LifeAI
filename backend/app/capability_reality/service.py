"""Life Capability Reality / Self-Model -- the deterministic boundary between "code/a binary
exists" and "Life may treat this as something she can actually do right now."

This module NEVER infers `status`. Every call site supplies it explicitly.
`verified_available` REQUIRES evidence that SUPPORTS the claim (see app.evidence_claim).
EVIDENCE EXISTS != CLAIM PROVEN.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence_claim import require_supporting_evidence_for_verified
from app.models.capability_reality import CapabilityObservationEvent, CapabilityRecord


class CapabilityEvidenceError(ValueError):
    """Raised when verified_available is requested without supporting evidence."""


def record_capability_observation(
    db: Session,
    *,
    owner_id: UUID,
    capability_key: str,
    domain: str,
    status: str,
    status_reason: str | None = None,
    authority: str = "unknown",
    required_permissions: list[str] | None = None,
    dependencies: list[str] | None = None,
    known_limitations: list[str] | None = None,
    confidence: float | None = None,
    agent_id: UUID | None = None,
    verification_evidence_id: UUID | None = None,
    success: bool | None = None,
    provenance: dict[str, Any] | None = None,
) -> CapabilityRecord:
    """Find-or-create capability row. verified_available requires supporting evidence."""

    if status == "verified_available":
        support = require_supporting_evidence_for_verified(
            db,
            owner_id=owner_id,
            capability_key=capability_key,
            verification_evidence_id=verification_evidence_id,
            success=success,
        )
        if not support.supports:
            raise CapabilityEvidenceError(
                "verified_available rejected: " + ",".join(support.reasons)
            )

    now = datetime.utcnow()
    record = db.execute(
        select(CapabilityRecord).where(
            CapabilityRecord.owner_id == owner_id,
            CapabilityRecord.capability_key == capability_key,
        )
    ).scalar_one_or_none()

    if (
        record is not None
        and record.status == "verified_available"
        and success is False
        and status == "verified_available"
    ):
        raise CapabilityEvidenceError(
            "OLD SUCCESS + NEW FAILURE != CURRENT VERIFIED SUCCESS"
        )

    is_new = record is None
    if record is None:
        record = CapabilityRecord(
            owner_id=owner_id, capability_key=capability_key, domain=domain
        )
        db.add(record)

    status_changed = is_new or record.status != status
    record.domain = domain
    record.status = status
    record.status_reason = status_reason
    record.authority = authority
    if required_permissions is not None:
        record.required_permissions = required_permissions
    if dependencies is not None:
        record.dependencies = dependencies
    if known_limitations is not None:
        record.known_limitations = known_limitations
    if confidence is not None:
        record.confidence = confidence
    if agent_id is not None:
        record.agent_id = agent_id
    if provenance is not None:
        record.provenance = provenance

    if verification_evidence_id is not None:
        record.last_verification_evidence_id = verification_evidence_id
        record.last_verified_at = now
    if success is True:
        record.last_success_at = now
    elif success is False:
        record.last_failure_at = now

    db.flush()

    event_type = (
        "status_changed"
        if status_changed
        else (
            "verification_recorded"
            if verification_evidence_id is not None
            else "success_recorded"
            if success is True
            else "failure_recorded"
            if success is False
            else "observation_reasserted"
        )
    )
    detail: dict[str, Any] = {
        "status": status,
        "status_reason": status_reason,
        "authority": authority,
    }
    if verification_evidence_id is not None:
        detail["verification_evidence_id"] = str(verification_evidence_id)
    if success is not None:
        detail["success"] = success
    db.add(
        CapabilityObservationEvent(
            owner_id=owner_id,
            capability_record_id=record.id,
            event_type=event_type,
            detail=detail,
        )
    )
    db.flush()
    return record


def record_capability_gap(
    db: Session,
    *,
    owner_id: UUID,
    capability_key: str,
    domain: str,
    reason: str,
    authority: str = "unknown",
    dependencies: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
) -> CapabilityRecord:
    record = record_capability_observation(
        db,
        owner_id=owner_id,
        capability_key=capability_key,
        domain=domain,
        status="planned",
        status_reason=reason,
        authority=authority,
        dependencies=dependencies,
        provenance=provenance,
    )
    db.add(
        CapabilityObservationEvent(
            owner_id=owner_id,
            capability_record_id=record.id,
            event_type="gap_recorded",
            detail={"reason": reason},
        )
    )
    db.flush()
    return record


def get_capability_reality(
    db: Session, *, owner_id: UUID, capability_key: str
) -> CapabilityRecord | None:
    return db.execute(
        select(CapabilityRecord).where(
            CapabilityRecord.owner_id == owner_id,
            CapabilityRecord.capability_key == capability_key,
        )
    ).scalar_one_or_none()


def list_capability_records(
    db: Session,
    *,
    owner_id: UUID,
    domain: str | None = None,
    status: str | None = None,
) -> list[CapabilityRecord]:
    stmt = select(CapabilityRecord).where(CapabilityRecord.owner_id == owner_id)
    if domain is not None:
        stmt = stmt.where(CapabilityRecord.domain == domain)
    if status is not None:
        stmt = stmt.where(CapabilityRecord.status == status)
    return list(db.execute(stmt.order_by(CapabilityRecord.capability_key)).scalars().all())


def list_capability_gaps(
    db: Session, *, owner_id: UUID, domain: str | None = None
) -> list[CapabilityRecord]:
    stmt = select(CapabilityRecord).where(
        CapabilityRecord.owner_id == owner_id,
        CapabilityRecord.status != "verified_available",
    )
    if domain is not None:
        stmt = stmt.where(CapabilityRecord.domain == domain)
    return list(db.execute(stmt.order_by(CapabilityRecord.capability_key)).scalars().all())
