"""AI-independent inserts for immutable governance observations.

The caller supplies classifications; this module neither invokes providers nor infers them.
Idempotency keys replay only byte-for-byte equivalent semantic records.  A reused key with
different content fails closed instead of silently returning unrelated evidence.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.intelligence_governance import (
    ClassificationBasis,
    IntelligenceEvidence,
    IntelligenceExecution,
    IntelligenceIdea,
    IntelligenceIdeaLink,
    IntelligenceInterpretation,
)


class IdempotencyConflict(ValueError):
    pass


def _same_or_conflict(existing: Any, expected: dict[str, Any]) -> Any:
    mismatches = [name for name, value in expected.items() if getattr(existing, name) != value]
    if mismatches:
        raise IdempotencyConflict(f"idempotency key reused with different fields: {', '.join(sorted(mismatches))}")
    return existing


def record_execution(
    db: Session,
    *,
    owner_id: UUID,
    task_id: UUID,
    idempotency_key: str,
    job_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
    agent_identity: str | None = None,
    execution_environment: str | None = None,
    available_tools: list[str] | None = None,
    capabilities: list[str] | None = None,
    work_strategy_id: str | None = None,
    role: str = "unknown",
    participation_mode: str = "unknown",
    domain: str | None = None,
    task_type: str | None = None,
    context: dict[str, Any] | None = None,
    classification_basis: str = ClassificationBasis.unknown.value,
) -> IntelligenceExecution:
    values = {
        "task_id": task_id, "job_id": job_id, "provider": provider, "model": model,
        "model_version": model_version, "agent_identity": agent_identity,
        "execution_environment": execution_environment, "available_tools": available_tools or [],
        "capabilities": capabilities or [], "work_strategy_id": work_strategy_id, "role": role,
        "participation_mode": participation_mode, "domain": domain, "task_type": task_type,
        "context": context or {}, "classification_basis": classification_basis,
    }
    existing = db.execute(select(IntelligenceExecution).where(
        IntelligenceExecution.owner_id == owner_id,
        IntelligenceExecution.idempotency_key == idempotency_key,
    )).scalar_one_or_none()
    if existing:
        return _same_or_conflict(existing, values)
    row = IntelligenceExecution(owner_id=owner_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def record_evidence(
    db: Session,
    *, owner_id: UUID, execution_id: UUID, evidence_kind: str, payload: dict[str, Any],
    source_type: str, source_ref: str, idempotency_key: str,
    observer_execution_id: UUID | None = None, task_event_id: UUID | None = None,
    recovery_record_id: UUID | None = None, review_kind: str = "unknown",
    deterministic: bool = False,
) -> IntelligenceEvidence:
    values = {
        "evidence_kind": evidence_kind, "payload": payload, "source_type": source_type,
        "source_ref": source_ref, "observer_execution_id": observer_execution_id,
        "task_event_id": task_event_id, "recovery_record_id": recovery_record_id,
        "review_kind": review_kind, "deterministic": deterministic,
    }
    existing = db.execute(select(IntelligenceEvidence).where(
        IntelligenceEvidence.owner_id == owner_id,
        IntelligenceEvidence.execution_id == execution_id,
        IntelligenceEvidence.idempotency_key == idempotency_key,
    )).scalar_one_or_none()
    if existing:
        return _same_or_conflict(existing, values)
    row = IntelligenceEvidence(owner_id=owner_id, execution_id=execution_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def record_interpretation(
    db: Session,
    *, owner_id: UUID, evidence_id: UUID, interpretation_kind: str,
    payload: dict[str, Any], method: str, classification_basis: str,
    idempotency_key: str, confidence: float | None = None,
    supersedes_id: UUID | None = None,
) -> IntelligenceInterpretation:
    values = {"interpretation_kind": interpretation_kind, "payload": payload, "method": method,
              "classification_basis": classification_basis, "confidence": confidence,
              "supersedes_id": supersedes_id}
    existing = db.execute(select(IntelligenceInterpretation).where(
        IntelligenceInterpretation.owner_id == owner_id,
        IntelligenceInterpretation.evidence_id == evidence_id,
        IntelligenceInterpretation.idempotency_key == idempotency_key,
    )).scalar_one_or_none()
    if existing:
        return _same_or_conflict(existing, values)
    row = IntelligenceInterpretation(owner_id=owner_id, evidence_id=evidence_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def record_idea(
    db: Session,
    *, owner_id: UUID, execution_id: UUID, idea_kind: str, content: str,
    idempotency_key: str, evidence_id: UUID | None = None, disposition: str = "unknown",
    disposition_reason: str | None = None, classification_basis: str = "unknown",
    confidence: float | None = None,
) -> IntelligenceIdea:
    values = {"idea_kind": idea_kind, "content": content, "evidence_id": evidence_id,
              "disposition": disposition, "disposition_reason": disposition_reason,
              "classification_basis": classification_basis, "confidence": confidence}
    existing = db.execute(select(IntelligenceIdea).where(
        IntelligenceIdea.owner_id == owner_id,
        IntelligenceIdea.execution_id == execution_id,
        IntelligenceIdea.idempotency_key == idempotency_key,
    )).scalar_one_or_none()
    if existing:
        return _same_or_conflict(existing, values)
    row = IntelligenceIdea(owner_id=owner_id, execution_id=execution_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def record_idea_link(
    db: Session, *, owner_id: UUID, from_idea_id: UUID, to_idea_id: UUID,
    relation: str, evidence_id: UUID | None = None,
) -> IntelligenceIdeaLink:
    existing = db.execute(select(IntelligenceIdeaLink).where(
        IntelligenceIdeaLink.owner_id == owner_id,
        IntelligenceIdeaLink.from_idea_id == from_idea_id,
        IntelligenceIdeaLink.to_idea_id == to_idea_id,
        IntelligenceIdeaLink.relation == relation,
    )).scalar_one_or_none()
    if existing:
        return _same_or_conflict(existing, {"evidence_id": evidence_id})
    row = IntelligenceIdeaLink(owner_id=owner_id, from_idea_id=from_idea_id,
                               to_idea_id=to_idea_id, relation=relation, evidence_id=evidence_id)
    db.add(row)
    db.flush()
    return row
