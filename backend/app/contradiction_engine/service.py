"""Stage K — Contradiction + assumption engine.

Kinds: CONTRADICTS, ASSUMPTION, FACT, SUPERSEDED, CONTEXT-SPECIFIC.
Never silently overwrites history — invalidation appends events and finds affected work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.concept_reconciliation import relate_concepts
from app.memory_work_linkage import find_affected_work
from app.models.project_entities import ProjectEntity
from app.models.structured_claim import StructuredClaim, StructuredClaimEvent

_KIND_MAP = {
    "CONTRADICTS": "contradicts",
    "ASSUMPTION": "assumption",
    "FACT": "fact",
    "SUPERSEDED": "superseded",
    "CONTEXT-SPECIFIC": "context_specific",
    "context_specific": "context_specific",
}


class ContradictionEngineError(ValueError):
    pass


@dataclass
class InvalidationResult:
    claim_id: uuid.UUID
    affected_work: list[dict] = field(default_factory=list)
    prior_status: str = ""
    new_status: str = "invalidated"


def _norm_kind(kind: str) -> str:
    key = kind.strip()
    mapped = _KIND_MAP.get(key) or _KIND_MAP.get(key.upper()) or key.lower().replace("-", "_")
    if mapped not in {"contradicts", "assumption", "fact", "superseded", "context_specific"}:
        raise ContradictionEngineError(f"unsupported kind: {kind}")
    return mapped


def _event(db: Session, *, owner_id: uuid.UUID, claim_id: uuid.UUID, event_type: str, detail: dict) -> None:
    db.add(
        StructuredClaimEvent(
            owner_id=owner_id,
            claim_id=claim_id,
            event_type=event_type,
            detail=detail,
        )
    )


def record_structured_claim(
    db: Session,
    *,
    owner_id: uuid.UUID,
    kind: str,
    statement: str,
    idempotency_key: str,
    confidence: float = 0.5,
    source: str = "unknown",
    dependent_refs: list[dict] | None = None,
    revalidation_trigger: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    contradicts_entity_id: uuid.UUID | None = None,
    supersedes_claim_id: uuid.UUID | None = None,
    provenance: dict | None = None,
) -> StructuredClaim:
    kind_n = _norm_kind(kind)
    existing = db.execute(
        select(StructuredClaim).where(
            StructuredClaim.owner_id == owner_id,
            StructuredClaim.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    if supersedes_claim_id is not None:
        old = db.execute(
            select(StructuredClaim)
            .where(StructuredClaim.id == supersedes_claim_id, StructuredClaim.owner_id == owner_id)
            .with_for_update()
        ).scalar_one_or_none()
        if old is None:
            raise ContradictionEngineError("superseded claim missing")
        if old.status == "active":
            old.status = "superseded"
            _event(
                db,
                owner_id=owner_id,
                claim_id=old.id,
                event_type="superseded",
                detail={"by": idempotency_key},
            )


    if related_entity_id is not None:
        ent = db.execute(
            select(ProjectEntity).where(
                ProjectEntity.id == related_entity_id, ProjectEntity.owner_id == owner_id
            )
        ).scalar_one_or_none()
        if ent is None:
            raise ContradictionEngineError(
                f"related_entity_id={related_entity_id} does not belong to owner_id={owner_id}"
            )
    if contradicts_entity_id is not None:
        ent = db.execute(
            select(ProjectEntity).where(
                ProjectEntity.id == contradicts_entity_id, ProjectEntity.owner_id == owner_id
            )
        ).scalar_one_or_none()
        if ent is None:
            raise ContradictionEngineError(
                f"contradicts_entity_id={contradicts_entity_id} does not belong to owner_id={owner_id}"
            )

    row = StructuredClaim(
        owner_id=owner_id,
        kind=kind_n,
        statement=statement,
        confidence=confidence,
        source=source,
        status="active",
        dependent_refs=list(dependent_refs or []),
        revalidation_trigger=revalidation_trigger,
        related_entity_id=related_entity_id,
        contradicts_entity_id=contradicts_entity_id,
        supersedes_claim_id=supersedes_claim_id,
        provenance=dict(provenance or {"stage": "K"}),
        idempotency_key=idempotency_key,
        last_validated_at=datetime.utcnow() if kind_n == "fact" else None,
    )
    db.add(row)
    db.flush()

    if kind_n == "contradicts" and related_entity_id and contradicts_entity_id and related_entity_id != contradicts_entity_id:
        relate_concepts(
            db,
            owner_id=owner_id,
            from_entity_id=related_entity_id,
            to_entity_id=contradicts_entity_id,
            relationship_type="contradicts",
            note=f"structured_claim:{row.id}",
        )

    _event(
        db,
        owner_id=owner_id,
        claim_id=row.id,
        event_type="created",
        detail={"kind": kind_n, "statement": statement[:200]},
    )
    db.flush()
    return row


def invalidate_assumption(
    db: Session,
    *,
    owner_id: uuid.UUID,
    claim_id: uuid.UUID,
    evidence_note: str,
) -> InvalidationResult:
    """New evidence invalidates an assumption — preserve history, find affected work."""
    row = db.execute(
        select(StructuredClaim)
        .where(StructuredClaim.id == claim_id, StructuredClaim.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ContradictionEngineError("claim missing")
    prior = row.status
    row.status = "invalidated"
    row.updated_at = datetime.utcnow()
    _event(
        db,
        owner_id=owner_id,
        claim_id=row.id,
        event_type="invalidated",
        detail={"evidence_note": evidence_note, "prior_status": prior},
    )
    db.flush()

    affected = find_affected_work(db, owner_id=owner_id, text=row.statement)
    from app.memory_work_linkage.types import AffectedWorkRef

    refs: list[AffectedWorkRef] = list(affected)
    for ref in row.dependent_refs or []:
        try:
            refs.append(
                AffectedWorkRef(
                    kind=str(ref.get("kind", "unknown")),
                    id=uuid.UUID(str(ref["id"])),
                    status=None,
                    score=1.0,
                    reason="dependent_ref",
                )
            )
        except (KeyError, ValueError, TypeError):
            continue

    return InvalidationResult(
        claim_id=row.id,
        affected_work=[{"kind": a.kind, "id": str(a.id), "reason": a.reason} for a in refs],
        prior_status=prior,
        new_status="invalidated",
    )


def list_claims(
    db: Session,
    *,
    owner_id: uuid.UUID,
    kind: str | None = None,
    status: str | None = "active",
) -> list[StructuredClaim]:
    stmt = select(StructuredClaim).where(StructuredClaim.owner_id == owner_id)
    if kind is not None:
        stmt = stmt.where(StructuredClaim.kind == _norm_kind(kind))
    if status is not None:
        stmt = stmt.where(StructuredClaim.status == status)
    return list(db.execute(stmt.order_by(StructuredClaim.created_at.desc())).scalars().all())
