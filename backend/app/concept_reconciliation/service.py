"""Stage B — Idea / concept reconciliation (deterministic, provider-free).

Canonical concepts remain `project_entities`. SAME collapses to one entity + optional alias.
Does not invent authority or authorize work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.concept_reconciliation.normalize import jaccard, normalize_concept_text, token_set
from app.models.project_entities import ProjectEntity, ProjectEntityAlias, ProjectEntityRelationship
from app.project_entities.service import (
    ProjectEntityError,
    list_current_project_entities,
    promote_interpretation_proposal,
    record_entity_relationship,
    record_interpretation_proposal,
)
from app.work_candidates.service import list_work_candidates

STAGE_B_RELATIONSHIP_TYPES = frozenset({
    "same", "partial_overlap", "related", "depends_on", "contradicts", "supersedes",
    "extends", "alternative", "reuses",
    # legacy retained
    "relates_to", "blocks", "answers", "duplicates", "derived_from",
})

# Exact fingerprint match → SAME. Token Jaccard in this open interval → PARTIAL_OVERLAP.
_PARTIAL_OVERLAP_MIN = 0.45
_PARTIAL_OVERLAP_MAX = 1.0  # exclusive of exact (exact handled separately)


class ConceptReconciliationError(ValueError):
    pass


@dataclass
class ClassificationHit:
    entity_id: uuid.UUID
    relationship_type: str  # same | partial_overlap
    score: float


@dataclass
class ReconcileResult:
    outcome: str  # created | reused_same | related
    canonical_entity_id: uuid.UUID
    relationship_type: str | None
    created_entity: bool
    created_work_candidate: bool
    alias_id: uuid.UUID | None = None


def find_same_concept(
    db: Session,
    *,
    owner_id: uuid.UUID,
    entity_type: str,
    title: str,
) -> ProjectEntity | None:
    norm = normalize_concept_text(title)
    if not norm:
        raise ConceptReconciliationError("title normalizes to empty")
    by_title = db.execute(
        select(ProjectEntity).where(
            ProjectEntity.owner_id == owner_id,
            ProjectEntity.entity_type == entity_type,
            ProjectEntity.title_normalized == norm,
            ProjectEntity.status.in_(("active", "proposed")),
        )
    ).scalar_one_or_none()
    if by_title is not None:
        return by_title
    alias = db.execute(
        select(ProjectEntityAlias).where(
            ProjectEntityAlias.owner_id == owner_id,
            ProjectEntityAlias.text_normalized == norm,
        )
    ).scalar_one_or_none()
    if alias is None:
        return None
    return db.execute(
        select(ProjectEntity).where(
            ProjectEntity.id == alias.entity_id,
            ProjectEntity.owner_id == owner_id,
            ProjectEntity.status.in_(("active", "proposed")),
        )
    ).scalar_one_or_none()


def classify_against_corpus(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str,
    entity_type: str | None = "idea",
) -> list[ClassificationHit]:
    """Deterministic classification against current project entities. No provider."""
    norm = normalize_concept_text(title)
    if not norm:
        return []
    tokens = token_set(title)
    hits: list[ClassificationHit] = []
    for entity in list_current_project_entities(db, owner_id=owner_id, entity_type=entity_type):
        entity_norm = entity.title_normalized or normalize_concept_text(entity.title)
        if entity_norm == norm:
            hits.append(ClassificationHit(entity_id=entity.id, relationship_type="same", score=1.0))
            continue
        # Also check aliases for this entity
        aliases = db.execute(
            select(ProjectEntityAlias).where(
                ProjectEntityAlias.owner_id == owner_id,
                ProjectEntityAlias.entity_id == entity.id,
            )
        ).scalars().all()
        if any(a.text_normalized == norm for a in aliases):
            hits.append(ClassificationHit(entity_id=entity.id, relationship_type="same", score=1.0))
            continue
        score = jaccard(tokens, token_set(entity.title))
        if _PARTIAL_OVERLAP_MIN <= score < _PARTIAL_OVERLAP_MAX:
            hits.append(ClassificationHit(entity_id=entity.id, relationship_type="partial_overlap", score=score))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def attach_alias(
    db: Session,
    *,
    owner_id: uuid.UUID,
    entity_id: uuid.UUID,
    raw_text: str,
    source_claim_id: uuid.UUID | None = None,
    provenance: dict | None = None,
) -> ProjectEntityAlias | None:
    """Bind an alternate wording to a canonical entity. Returns None if wording already is the title."""
    entity = db.execute(
        select(ProjectEntity).where(ProjectEntity.id == entity_id, ProjectEntity.owner_id == owner_id)
    ).scalar_one_or_none()
    if entity is None:
        raise ConceptReconciliationError("entity missing or belongs to another owner")
    norm = normalize_concept_text(raw_text)
    if not norm:
        raise ConceptReconciliationError("alias text normalizes to empty")
    # Skip only when the surface form is already exactly the entity title.
    if raw_text.strip() == entity.title.strip():
        return None
    existing = db.execute(
        select(ProjectEntityAlias).where(
            ProjectEntityAlias.owner_id == owner_id,
            ProjectEntityAlias.text_normalized == norm,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.entity_id != entity_id:
            raise ConceptReconciliationError("alias already bound to a different concept")
        return existing
    # Unique (owner, text_normalized) — if norm equals the entity fingerprint, use a
    # surface-form-qualified key so alternate punctuation/casing remains inspectable.
    alias_norm = norm
    if alias_norm == (entity.title_normalized or normalize_concept_text(entity.title)):
        alias_norm = f"alias:{norm}:{raw_text.strip().casefold()}"
        # Keep within varchar(512)
        alias_norm = alias_norm[:512]
        existing_surface = db.execute(
            select(ProjectEntityAlias).where(
                ProjectEntityAlias.owner_id == owner_id,
                ProjectEntityAlias.text_normalized == alias_norm,
            )
        ).scalar_one_or_none()
        if existing_surface is not None:
            return existing_surface
    row = ProjectEntityAlias(
        owner_id=owner_id,
        entity_id=entity_id,
        raw_text=raw_text.strip(),
        text_normalized=alias_norm,
        source_claim_id=source_claim_id,
        provenance=dict(provenance or {}),
    )
    db.add(row)
    db.flush()
    return row


def relate_concepts(
    db: Session,
    *,
    owner_id: uuid.UUID,
    from_entity_id: uuid.UUID,
    to_entity_id: uuid.UUID,
    relationship_type: str,
    note: str | None = None,
) -> ProjectEntityRelationship:
    if relationship_type not in STAGE_B_RELATIONSHIP_TYPES:
        raise ConceptReconciliationError(f"unsupported relationship_type: {relationship_type}")
    if relationship_type == "same":
        raise ConceptReconciliationError("SAME must collapse via reuse/alias, not a same-edge between two entities")
    existing = db.execute(
        select(ProjectEntityRelationship).where(
            ProjectEntityRelationship.owner_id == owner_id,
            ProjectEntityRelationship.from_entity_id == from_entity_id,
            ProjectEntityRelationship.to_entity_id == to_entity_id,
            ProjectEntityRelationship.relationship_type == relationship_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    try:
        return record_entity_relationship(
            db,
            owner_id=owner_id,
            from_entity_id=from_entity_id,
            to_entity_id=to_entity_id,
            relationship_type=relationship_type,
            note=note,
        )
    except ProjectEntityError as exc:
        raise ConceptReconciliationError(str(exc)) from exc


def _count_work_candidates_for_entity(db: Session, *, owner_id: uuid.UUID, entity_id: uuid.UUID) -> int:
    return sum(1 for c in list_work_candidates(db, owner_id=owner_id) if c.source_entity_id == entity_id)


def reconcile_and_promote_idea(
    db: Session,
    *,
    owner_id: uuid.UUID,
    proposal_id: uuid.UUID,
    title: str,
    entity_type: str = "idea",
    entity_idempotency_key: str,
    authority: str = "founder",
    basis: str = "manual",
    summary: str | None = None,
    confidence: float | None = None,
) -> ReconcileResult:
    """Promote with SAME-collapse (via promote_interpretation_proposal) and optional PARTIAL_OVERLAP edges."""
    before_ids = {c.source_entity_id for c in list_work_candidates(db, owner_id=owner_id)}
    existing_before = find_same_concept(db, owner_id=owner_id, entity_type=entity_type, title=title)
    overlaps = [
        h
        for h in classify_against_corpus(db, owner_id=owner_id, title=title, entity_type=entity_type)
        if h.relationship_type == "partial_overlap"
    ]
    try:
        _proposal, entity = promote_interpretation_proposal(
            db,
            owner_id=owner_id,
            proposal_id=proposal_id,
            entity_type=entity_type,
            title=title,
            entity_idempotency_key=entity_idempotency_key,
            summary=summary,
            authority=authority,
            basis=basis,
            confidence=confidence,
        )
    except ProjectEntityError as exc:
        raise ConceptReconciliationError(str(exc)) from exc

    created_entity = (
        existing_before is None and entity.idempotency_key == entity_idempotency_key
    )
    # Race loser recovers onto winner (different idempotency_key) → reused_same, not created.
    if created_entity:
        for hit in overlaps:
            if hit.entity_id != entity.id:
                relate_concepts(
                    db,
                    owner_id=owner_id,
                    from_entity_id=entity.id,
                    to_entity_id=hit.entity_id,
                    relationship_type="partial_overlap",
                    note=f"jaccard={hit.score:.2f}",
                )

    after_count = _count_work_candidates_for_entity(db, owner_id=owner_id, entity_id=entity.id)
    created_wc = entity.id not in before_ids and after_count > 0
    alias_row = db.execute(
        select(ProjectEntityAlias).where(
            ProjectEntityAlias.owner_id == owner_id,
            ProjectEntityAlias.entity_id == entity.id,
            ProjectEntityAlias.text_normalized == normalize_concept_text(title),
        )
    ).scalar_one_or_none()

    if not created_entity:
        outcome = "reused_same"
        rel = "same"
    elif overlaps:
        outcome = "related"
        rel = "partial_overlap"
    else:
        outcome = "created"
        rel = None
    return ReconcileResult(
        outcome=outcome,
        canonical_entity_id=entity.id,
        relationship_type=rel,
        created_entity=created_entity,
        created_work_candidate=created_wc,
        alias_id=alias_row.id if alias_row is not None else None,
    )
