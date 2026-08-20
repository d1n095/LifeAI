"""Life Project Entities / Interpretation Queue -- the staging layer between a live signal
producer (`app.rag.claims.extract_claims_for_document()`'s own claim_type classification) and
trusted structured project understanding (`project_entities`). See migration 0054's own module
docstring for the full architecture.

Hard rule, structural not just documented: `record_interpretation_proposal()` NEVER writes to
`project_entities`, directly or indirectly. The ONLY function in this module that can create a
`ProjectEntity` is `promote_interpretation_proposal()`, and it ALWAYS requires the caller to
supply `authority`/`basis` explicitly -- the proposal's own `classifier_confidence` (the
source claim's own objective, grounding-based confidence bucket, never the extracting model's
self-report) is never silently copied into the entity's `authority`. A
`classifier_confidence="certain"` claim is still, at most, `authority="ai_interpretation"` or
`authority="deterministic_source"` unless a human reviewer explicitly asserts otherwise --
promotion is where that judgment call belongs, never automatic."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project_entities import InterpretationProposal, ProjectEntity, ProjectEntityRelationship


class ProjectEntityError(ValueError):
    pass


def _same(row: InterpretationProposal, values: dict[str, Any]) -> InterpretationProposal:
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise ProjectEntityError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
    return row


def record_interpretation_proposal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_claim_id: uuid.UUID,
    proposed_entity_type: str,
    idempotency_key: str,
    classifier_strategy: str = "unknown",
    classifier_confidence: str = "unknown",
    classifier_reasoning: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> InterpretationProposal:
    """Records ONE interpretation proposal -- never a claim about the project, only a claim
    that a signal producer noticed a claim that might be worth turning into structured
    understanding. Safe to call from a live extraction hot path (see
    `app/rag/claims.py`'s own `extract_claims_for_document()` integration): this function
    never raises for "the proposal turned out to be noise" -- that judgment happens later,
    explicitly, via `dismiss_interpretation_proposal()`/`promote_interpretation_proposal()`,
    never here."""

    values: dict[str, Any] = dict(
        source_claim_id=source_claim_id, proposed_entity_type=proposed_entity_type,
        classifier_strategy=classifier_strategy, classifier_confidence=classifier_confidence,
        classifier_reasoning=classifier_reasoning, provenance=provenance or {},
    )
    existing = db.execute(
        select(InterpretationProposal).where(InterpretationProposal.owner_id == owner_id, InterpretationProposal.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)

    row = InterpretationProposal(owner_id=owner_id, idempotency_key=idempotency_key, status="unreviewed", **values)
    db.add(row)
    db.flush()
    return row


def dismiss_interpretation_proposal(db: Session, *, owner_id: uuid.UUID, proposal_id: uuid.UUID, reason: str) -> InterpretationProposal:
    """An explicit "this proposal was noise, not worth promoting" outcome -- never deletes the
    row, so the same non-proposal is not re-surfaced for review indefinitely without a durable
    record that it was already considered."""

    row = db.execute(
        select(InterpretationProposal).where(InterpretationProposal.id == proposal_id, InterpretationProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProjectEntityError("interpretation proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ProjectEntityError(f"interpretation proposal is already {row.status}, not unreviewed")
    row.status = "dismissed"
    row.dismissed_reason = reason
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def promote_interpretation_proposal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    proposal_id: uuid.UUID,
    entity_type: str,
    title: str,
    entity_idempotency_key: str,
    summary: str | None = None,
    authority: str = "unknown",
    basis: str = "unknown",
    confidence: float | None = None,
    decided_by: str | None = None,
    decided_at: datetime | None = None,
) -> tuple[InterpretationProposal, ProjectEntity]:
    """The ONLY path from an interpretation proposal to real project understanding -- SIGNAL
    PRODUCER != TRUTH WRITER enforced here, not just in the schema's own CHECK constraint.
    `authority`/`basis` are the caller's own explicit assertion (this function has no default
    that reads them off the proposal itself) -- promoting a `classifier_confidence="certain"`
    proposal does not imply `authority="founder"`; a reviewer who confirms the claim really
    does reflect a project decision passes `authority="founder"` themselves, deliberately, the
    same way `app.founder_memory_signals.promote_candidate_signal()` already requires. `title`/
    `summary` are likewise always caller-supplied, never auto-derived from the claim's raw
    text -- a reviewer may summarize, quote verbatim, or add context; either way it is a
    deliberate, reviewed act of writing, not a copy."""

    row = db.execute(
        select(InterpretationProposal).where(InterpretationProposal.id == proposal_id, InterpretationProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProjectEntityError("interpretation proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ProjectEntityError(f"interpretation proposal is already {row.status}, not unreviewed")

    entity = ProjectEntity(
        owner_id=owner_id, entity_type=entity_type, title=title, summary=summary,
        derived_from_claim_id=row.source_claim_id, idempotency_key=entity_idempotency_key,
        authority=authority, basis=basis, confidence=confidence,
        decided_by=decided_by, decided_at=decided_at,
        provenance={"promoted_from_interpretation_proposal_id": str(row.id)},
    )
    db.add(entity)
    db.flush()
    row.status = "promoted"
    row.promoted_to_entity_id = entity.id
    row.updated_at = datetime.utcnow()
    db.flush()
    return row, entity


def get_interpretation_proposal(db: Session, *, owner_id: uuid.UUID, proposal_id: uuid.UUID) -> InterpretationProposal | None:
    return db.execute(select(InterpretationProposal).where(InterpretationProposal.id == proposal_id, InterpretationProposal.owner_id == owner_id)).scalar_one_or_none()


def list_interpretation_proposals(db: Session, *, owner_id: uuid.UUID, status: str | None = None, proposed_entity_type: str | None = None) -> list[InterpretationProposal]:
    stmt = select(InterpretationProposal).where(InterpretationProposal.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(InterpretationProposal.status == status)
    if proposed_entity_type is not None:
        stmt = stmt.where(InterpretationProposal.proposed_entity_type == proposed_entity_type)
    return list(db.execute(stmt.order_by(InterpretationProposal.observed_at)).scalars().all())


def list_unreviewed_interpretation_proposals(db: Session, *, owner_id: uuid.UUID) -> list[InterpretationProposal]:
    return list_interpretation_proposals(db, owner_id=owner_id, status="unreviewed")


def get_project_entity(db: Session, *, owner_id: uuid.UUID, entity_id: uuid.UUID) -> ProjectEntity | None:
    return db.execute(select(ProjectEntity).where(ProjectEntity.id == entity_id, ProjectEntity.owner_id == owner_id)).scalar_one_or_none()


def list_current_project_entities(db: Session, *, owner_id: uuid.UUID, entity_type: str | None = None) -> list[ProjectEntity]:
    """The safe-by-default "give me what's currently true" query -- the same role
    `list_current_diagnoses()`/`list_current_founder_memory()` already play for their sibling
    foundations. Excludes `historical`/`superseded`/`disputed`, matching their own precedent
    for what "current" means."""

    stmt = select(ProjectEntity).where(ProjectEntity.owner_id == owner_id, ProjectEntity.status.in_(("active", "proposed")))
    if entity_type is not None:
        stmt = stmt.where(ProjectEntity.entity_type == entity_type)
    return list(db.execute(stmt.order_by(ProjectEntity.created_at)).scalars().all())


def mark_project_entity_superseded(db: Session, *, owner_id: uuid.UUID, entity_id: uuid.UUID, superseded_by_entity_id: uuid.UUID) -> ProjectEntity:
    """Marks an entity as `superseded` -- never deletes or mutates its own content, matching
    every other "derived knowledge" foundation's supersession discipline. The NEW entity is
    expected to already exist and set its own `supersedes_entity_id` to this row's id; this
    function only flips the OLD row's status, it does not create the new row itself (that is
    an ordinary `record_interpretation_proposal()` -> `promote_interpretation_proposal()` pass
    with `supersedes_entity_id` passed through)."""

    row = db.execute(
        select(ProjectEntity).where(ProjectEntity.id == entity_id, ProjectEntity.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProjectEntityError("project entity is missing or belongs to another owner")
    row.status = "superseded"
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def record_entity_relationship(
    db: Session, *, owner_id: uuid.UUID, from_entity_id: uuid.UUID, to_entity_id: uuid.UUID, relationship_type: str, note: str | None = None,
) -> ProjectEntityRelationship:
    if from_entity_id == to_entity_id:
        raise ProjectEntityError("a project entity cannot have a relationship to itself")
    row = ProjectEntityRelationship(
        owner_id=owner_id, from_entity_id=from_entity_id, to_entity_id=to_entity_id,
        relationship_type=relationship_type, note=note,
    )
    db.add(row)
    db.flush()
    return row


def list_entity_relationships(db: Session, *, owner_id: uuid.UUID, entity_id: uuid.UUID) -> list[ProjectEntityRelationship]:
    stmt = select(ProjectEntityRelationship).where(
        ProjectEntityRelationship.owner_id == owner_id,
        (ProjectEntityRelationship.from_entity_id == entity_id) | (ProjectEntityRelationship.to_entity_id == entity_id),
    )
    return list(db.execute(stmt.order_by(ProjectEntityRelationship.created_at)).scalars().all())
