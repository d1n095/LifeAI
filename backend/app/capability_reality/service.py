"""Life Capability Reality / Self-Model -- the deterministic boundary between "code/a binary
exists" and "Life may treat this as something she can actually do right now."

This module NEVER infers `status`. Every call site supplies it explicitly -- the same
"caller supplies classifications; this module neither invokes providers nor infers them"
doctrine `app.intelligence_governance.service`'s own module docstring already establishes.
There is no code path here that looks at a filesystem, a provider, or an adapter and decides
for itself that something is `verified_available`. `sync_agent_adapter_capability()` below is
the one exception worth naming explicitly BECAUSE of how it stays honest: it translates
`app.agent_coordination.adapter_config.adapter_availability()`'s own facts into a capability
record, but it can only ever produce `configured_disabled`/`configured_unavailable`/`unknown`
-- an executable being found AND enabled is still not verification. Only a caller who has
observed a REAL successful use (a real dispatch, a real test run, a real agent action) may
ever pass `status="verified_available"`.

`record_capability_observation()` is a plain find-or-create-and-update over the owner-scoped
`(owner_id, capability_key)` identity -- not append-only itself (the current-state row is
meant to be queried cheaply, not reconstructed from history every time), but every call also
appends an immutable `CapabilityObservationEvent` so the full history remains auditable. This
mirrors `app.agent_coordination.service.acquire_lease()`'s own "live mutable row + append-only
event log" split exactly."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.capability_reality import CapabilityObservationEvent, CapabilityRecord


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
    """Finds-or-creates the live `capability_records` row for `(owner_id, capability_key)` and
    applies exactly the fields the caller supplied -- every field here is an explicit
    assertion, never a default the module invents on the caller's behalf beyond the model's
    own `status="unknown"`/`authority="unknown"` column defaults (UNKNOWN is always valid, per
    the mission this foundation answers to).

    `verification_evidence_id`, if supplied, sets `last_verification_evidence_id` and
    `last_verified_at=now()` -- it does NOT itself change `status`; the caller still supplies
    `status` explicitly in the SAME call, so a capability can never silently become
    `verified_available` merely because SOME evidence reference was attached.

    `success=True`/`success=False` records `last_success_at`/`last_failure_at` respectively --
    again, purely observational bookkeeping, never a status inference.

    Always appends exactly one `CapabilityObservationEvent` recording what changed."""

    now = datetime.utcnow()
    record = db.execute(
        select(CapabilityRecord).where(CapabilityRecord.owner_id == owner_id, CapabilityRecord.capability_key == capability_key)
    ).scalar_one_or_none()

    is_new = record is None
    if record is None:
        record = CapabilityRecord(owner_id=owner_id, capability_key=capability_key, domain=domain)
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

    # The label describes what actually happened in THIS call -- "status_changed" is only ever
    # honest when the status genuinely differs from before (or this is a brand-new record);
    # re-asserting the SAME status with no new verification/success info is a real, distinct
    # fact ("observation_reasserted"), never mislabeled as a change that didn't happen.
    event_type = "status_changed" if status_changed else (
        "verification_recorded" if verification_evidence_id is not None else
        "success_recorded" if success is True else
        "failure_recorded" if success is False else
        "observation_reasserted"
    )
    # The event_type label picks ONE headline for what this observation was mainly about, but
    # detail always carries every fact this call actually asserted -- never just the fact that
    # happened to win the label -- so the append-only history stays self-sufficient and never
    # has to fall back on the live row's own (overwritable) last_verification_evidence_id/
    # last_success_at/last_failure_at to answer "what did THIS specific observation assert."
    detail: dict[str, Any] = {"status": status, "status_reason": status_reason, "authority": authority}
    if verification_evidence_id is not None:
        detail["verification_evidence_id"] = str(verification_evidence_id)
    if success is not None:
        detail["success"] = success
    db.add(CapabilityObservationEvent(owner_id=owner_id, capability_record_id=record.id, event_type=event_type, detail=detail))
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
    """The explicit "capability missing/unusable -> record a grounded gap" path -- a thin,
    named wrapper over `record_capability_observation(status="planned")` so a caller recording
    a gap can never accidentally omit a reason or drift the status value. Never pretends the
    capability exists; `status="planned"` is exactly as far as this function can ever go."""

    record = record_capability_observation(
        db, owner_id=owner_id, capability_key=capability_key, domain=domain, status="planned",
        status_reason=reason, authority=authority, dependencies=dependencies, provenance=provenance,
    )
    db.add(CapabilityObservationEvent(
        owner_id=owner_id, capability_record_id=record.id, event_type="gap_recorded", detail={"reason": reason},
    ))
    db.flush()
    return record


def get_capability_reality(db: Session, *, owner_id: UUID, capability_key: str) -> CapabilityRecord | None:
    return db.execute(
        select(CapabilityRecord).where(CapabilityRecord.owner_id == owner_id, CapabilityRecord.capability_key == capability_key)
    ).scalar_one_or_none()


def list_capability_records(db: Session, *, owner_id: UUID, domain: str | None = None, status: str | None = None) -> list[CapabilityRecord]:
    stmt = select(CapabilityRecord).where(CapabilityRecord.owner_id == owner_id)
    if domain is not None:
        stmt = stmt.where(CapabilityRecord.domain == domain)
    if status is not None:
        stmt = stmt.where(CapabilityRecord.status == status)
    return list(db.execute(stmt.order_by(CapabilityRecord.capability_key)).scalars().all())


def list_capability_gaps(db: Session, *, owner_id: UUID, domain: str | None = None) -> list[CapabilityRecord]:
    """Every capability NOT `verified_available` -- planned, configured-but-disabled,
    configured-but-unavailable, or genuinely unknown. This is the query
    `list_capability_records(..., status="verified_available")`'s complement answers "what can
    Life actually do right now" for; this one answers "what can't she, and why.\""""

    stmt = select(CapabilityRecord).where(CapabilityRecord.owner_id == owner_id, CapabilityRecord.status != "verified_available")
    if domain is not None:
        stmt = stmt.where(CapabilityRecord.domain == domain)
    return list(db.execute(stmt.order_by(CapabilityRecord.capability_key)).scalars().all())
