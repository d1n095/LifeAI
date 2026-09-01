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

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.knowledge_claim import KnowledgeClaim
from app.models.project_entities import InterpretationProposal, ProjectEntity, ProjectEntityRelationship

logger = logging.getLogger(__name__)

# The subset of entity_type this codebase's own plan (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md
# §4.2's own "task_reference-typen länkar till en BEFINTLIG Task-rad" note) treats as
# potentially actionable work, as opposed to a plain recorded fact (vision_statement,
# open_question) -- the exact subset migration 0055's own module docstring routes into
# app.work_candidates.
_ACTIONABLE_ENTITY_TYPES = {"idea", "decision", "task_reference"}


def _normalize_title(title: str) -> str:
    from app.concept_reconciliation.normalize import normalize_concept_text

    return normalize_concept_text(title)

def _record_work_candidate_if_actionable(db: Session, *, owner_id: uuid.UUID, entity: ProjectEntity) -> None:
    """Purely observational, same doctrine as app/rag/claims.py's own interpretation-proposal
    integration one level up: never changes promote_interpretation_proposal()'s own result,
    never raises into the caller. Writes ONLY to work_candidates -- a staging table nothing
    treats as authorized work -- never calls create_goal(). See migration 0055's own module
    docstring: DERIVED WORK CANDIDATE != AUTHORIZED WORK != EXECUTABLE WORK.

    Uses a SAVEPOINT (db.begin_nested()), not a top-level commit/rollback -- unlike the
    claims.py/chat.py call sites, promote_interpretation_proposal() itself never commits
    (leaves that to ITS OWN caller, same as promote_candidate_signal()), so a plain
    db.commit()/db.rollback() here would either surprise-commit the caller's still-open
    transaction or, on failure, roll back the entity/proposal promotion this function is
    supposed to be a side effect OF, not a co-equal risk to. A SAVEPOINT failure rolls back
    only this nested unit of work, exactly like app/rag/memory_source.py's own established
    SAVEPOINT precedent."""

    if entity.entity_type not in _ACTIONABLE_ENTITY_TYPES:
        return
    try:
        from app.work_candidates import record_work_candidate

        savepoint = db.begin_nested()
        try:
            record_work_candidate(
                db, owner_id=owner_id, source_entity_id=entity.id, title=entity.title,
                rationale=entity.summary, classifier_strategy="project_entity_promotion_v1",
                classifier_confidence=entity.confidence,
                idempotency_key=f"project-entity-promotion:{entity.id}",
            )
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise
    except Exception:
        logger.warning("failed to record work candidate for project entity %s (non-fatal)", entity.id, exc_info=True)


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
    never here.

    Fails closed BEFORE any write if `source_claim_id` does not structurally belong to
    `owner_id` -- a clear, typed error here, with the database's own composite FK (migration
    0056) as the final backstop regardless of what this check does or doesn't catch."""

    claim = db.execute(select(KnowledgeClaim).where(KnowledgeClaim.id == source_claim_id, KnowledgeClaim.owner_id == owner_id)).scalar_one_or_none()
    if claim is None:
        raise ProjectEntityError(f"source_claim_id={source_claim_id} does not belong to owner_id={owner_id}")

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
    supersedes_entity_id: uuid.UUID | None = None,
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
    deliberate, reviewed act of writing, not a copy.

    `supersedes_entity_id`, when given, must reference an EXISTING entity belonging to the
    SAME owner (fails closed otherwise, before any write) -- this is how the new entity's own
    `supersedes_entity_id` column actually gets set. Pass the same id to
    `mark_project_entity_superseded()` afterward to flip the OLD entity's status; that function
    now verifies the new entity's `supersedes_entity_id` genuinely points back at it before
    doing so, rather than trusting the caller's own bookkeeping."""

    row = db.execute(
        select(InterpretationProposal).where(InterpretationProposal.id == proposal_id, InterpretationProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProjectEntityError("interpretation proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ProjectEntityError(f"interpretation proposal is already {row.status}, not unreviewed")

    if supersedes_entity_id is not None:
        superseded_entity = db.execute(
            select(ProjectEntity).where(ProjectEntity.id == supersedes_entity_id, ProjectEntity.owner_id == owner_id)
        ).scalar_one_or_none()
        if superseded_entity is None:
            raise ProjectEntityError(f"supersedes_entity_id={supersedes_entity_id} does not belong to owner_id={owner_id}")

    # Stage B: SAME collapse — differently-worded duplicates reuse the canonical entity.
    title_normalized = _normalize_title(title)
    if not title_normalized:
        raise ProjectEntityError("title normalizes to empty")
    from app.concept_reconciliation.service import attach_alias, find_same_concept

    existing = find_same_concept(db, owner_id=owner_id, entity_type=entity_type, title=title)
    if existing is not None and supersedes_entity_id is None:
        row.status = "promoted"
        row.promoted_to_entity_id = existing.id
        row.updated_at = datetime.utcnow()
        db.flush()
        attach_alias(
            db,
            owner_id=owner_id,
            entity_id=existing.id,
            raw_text=title,
            source_claim_id=row.source_claim_id,
            provenance={"via": "promote_interpretation_proposal_same_collapse", "proposal_id": str(row.id)},
        )
        # Do NOT create a second work_candidate for the same canonical entity.
        return row, existing

    # Concurrent SAME-collapse: unique index stops duplicate canonical concepts, but the
    # loser must not get an unhandled IntegrityError / poisoned session. Recover via
    # SAVEPOINT → re-find → collapse onto the winner (same shape as create_job idempotency).
    entity = ProjectEntity(
        owner_id=owner_id, entity_type=entity_type, title=title, title_normalized=title_normalized, summary=summary,
        derived_from_claim_id=row.source_claim_id, idempotency_key=entity_idempotency_key,
        authority=authority, basis=basis, confidence=confidence,
        decided_by=decided_by, decided_at=decided_at, supersedes_entity_id=supersedes_entity_id,
        provenance={"promoted_from_interpretation_proposal_id": str(row.id)},
    )
    savepoint = db.begin_nested()
    try:
        db.add(entity)
        db.flush()
        savepoint.commit()
    except IntegrityError as exc:
        constraint_name = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
        savepoint.rollback()
        if constraint_name != "uq_project_entities_current_fingerprint":
            raise
        if supersedes_entity_id is not None:
            # Explicit supersession path is not a SAME-collapse race — fail closed.
            raise
        winner = find_same_concept(db, owner_id=owner_id, entity_type=entity_type, title=title)
        if winner is None:
            raise
        row.status = "promoted"
        row.promoted_to_entity_id = winner.id
        row.updated_at = datetime.utcnow()
        db.flush()
        attach_alias(
            db,
            owner_id=owner_id,
            entity_id=winner.id,
            raw_text=title,
            source_claim_id=row.source_claim_id,
            provenance={
                "via": "promote_interpretation_proposal_same_collapse_race_recovery",
                "proposal_id": str(row.id),
            },
        )
        return row, winner

    row.status = "promoted"
    row.promoted_to_entity_id = entity.id
    row.updated_at = datetime.utcnow()
    db.flush()
    _record_work_candidate_if_actionable(db, owner_id=owner_id, entity=entity)
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
    every other "derived knowledge" foundation's supersession discipline. The NEW entity must
    ALREADY exist, belong to the same owner, and have its own `supersedes_entity_id` already
    set to this row's id (pass `supersedes_entity_id=entity_id` to
    `promote_interpretation_proposal()` when creating it) -- this function VERIFIES that
    durable new->old link actually exists before flipping the old row's status, it does not
    set the link itself and does not trust the caller's own bookkeeping. Previously this
    parameter was accepted but silently ignored, letting a test (or a real caller) believe the
    historical edge existed when it never did -- fixed to fail closed instead."""

    row = db.execute(
        select(ProjectEntity).where(ProjectEntity.id == entity_id, ProjectEntity.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProjectEntityError("project entity is missing or belongs to another owner")

    superseding_entity = db.execute(
        select(ProjectEntity).where(ProjectEntity.id == superseded_by_entity_id, ProjectEntity.owner_id == owner_id)
    ).scalar_one_or_none()
    if superseding_entity is None:
        raise ProjectEntityError(f"superseded_by_entity_id={superseded_by_entity_id} does not belong to owner_id={owner_id}")
    if superseding_entity.supersedes_entity_id != row.id:
        raise ProjectEntityError(
            f"entity {superseded_by_entity_id} does not declare supersedes_entity_id={row.id} "
            f"(got {superseding_entity.supersedes_entity_id}) -- pass supersedes_entity_id={row.id} "
            f"to promote_interpretation_proposal() when creating the superseding entity"
        )

    row.status = "superseded"
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def record_entity_relationship(
    db: Session, *, owner_id: uuid.UUID, from_entity_id: uuid.UUID, to_entity_id: uuid.UUID, relationship_type: str, note: str | None = None,
) -> ProjectEntityRelationship:
    """Fails closed BEFORE any write if either endpoint does not structurally belong to
    `owner_id` -- a clear, typed error here, with the database's own composite FK (migration
    0056) as the final backstop."""

    if from_entity_id == to_entity_id:
        raise ProjectEntityError("a project entity cannot have a relationship to itself")
    for label, entity_id in (("from_entity_id", from_entity_id), ("to_entity_id", to_entity_id)):
        if db.execute(select(ProjectEntity.id).where(ProjectEntity.id == entity_id, ProjectEntity.owner_id == owner_id)).scalar_one_or_none() is None:
            raise ProjectEntityError(f"{label}={entity_id} does not belong to owner_id={owner_id}")
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
