"""Stage H — synthetic durable history + quality query answers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note, founder_correct_memory_note
from app.memory_work_linkage import TimingClass, apply_memory_work_linkage
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.founder_memory import FounderMemoryNote
from app.models.knowledge_claim import KnowledgeClaim
from app.models.project_entities import ProjectEntity
from app.models.work_candidate import WorkCandidate
from app.project_entities import record_interpretation_proposal
from app.temporal_intelligence import RecapWindow, build_recap


@dataclass
class HistoryStressSeedResult:
    owner_id: uuid.UUID
    note_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    entity_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    candidate_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass
class HistoryQueryAnswer:
    current: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    affected_work: list[str] = field(default_factory=list)
    recent_changes: list[str] = field(default_factory=list)
    historical_evolution: list[str] = field(default_factory=list)


def _promote(db, owner_id, title, key):
    document = Document(title="h", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=owner_id, source_id=document.id, claim_text=title, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db, owner_id=owner_id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key=f"h-prop-{key}"
    )
    db.flush()
    return reconcile_and_promote_idea(
        db, owner_id=owner_id, proposal_id=proposal.id, title=title, entity_idempotency_key=f"h-ent-{key}"
    )


def seed_synthetic_history(db: Session, *, owner_id: uuid.UUID) -> HistoryStressSeedResult:
    """Create durable synthetic history covering duplicates, corrections, supersessions, parks, contradictions."""
    result = HistoryStressSeedResult(owner_id=owner_id)

    # Duplicate ideas under different wording
    a = _promote(db, owner_id, "Use Postgres for MainAI memory storage", "dup-a")
    b = _promote(db, owner_id, "use postgres for mainai memory storage!", "dup-b")
    result.entity_ids["canonical_postgres"] = a.canonical_entity_id
    result.entity_ids["dup_attempt"] = b.canonical_entity_id

    # Corrected terminology
    original, _ = founder_add_memory_note(
        db, owner_id=owner_id, content="Use MongoDB for sessions", note_type="decision", idempotency_key=f"h-mongo-{uuid.uuid4()}"
    )
    corrected, _ = founder_correct_memory_note(
        db, owner_id=owner_id, note_id=original.id, content="Use Postgres for sessions", idempotency_key=f"h-pg-{uuid.uuid4()}"
    )
    result.note_ids["superseded_mongo"] = original.id
    result.note_ids["current_postgres_sessions"] = corrected.id

    # Parked / LATER idea
    later, _ = founder_add_memory_note(
        db, owner_id=owner_id, content="Quarterly archive export someday", note_type="goal", idempotency_key=f"h-later-{uuid.uuid4()}"
    )
    _promote(db, owner_id, "Quarterly archive export someday", "later")
    link = apply_memory_work_linkage(db, owner_id=owner_id, note_id=later.id, timing=TimingClass.LATER)
    result.note_ids["parked_later"] = later.id
    result.candidate_ids.extend(link.created_candidate_ids)

    # Active work signal
    active, _ = founder_add_memory_note(
        db, owner_id=owner_id, content="Harden memory work linkage path", note_type="decision", idempotency_key=f"h-active-{uuid.uuid4()}"
    )
    result.note_ids["active"] = active.id
    apply_memory_work_linkage(db, owner_id=owner_id, note_id=active.id)

    # Contradiction pair
    plan_e = _promote(db, owner_id, "Keep single-region deploy", "plan")
    new_e = _promote(db, owner_id, "Multi-region active-active required", "multi")
    contra, _ = founder_add_memory_note(
        db, owner_id=owner_id, content="Multi-region active-active required", note_type="decision", idempotency_key=f"h-contra-{uuid.uuid4()}"
    )
    apply_memory_work_linkage(db, owner_id=owner_id, note_id=contra.id, contradict_entity_id=plan_e.canonical_entity_id)
    result.note_ids["contradiction"] = contra.id
    result.entity_ids["plan"] = plan_e.canonical_entity_id
    result.entity_ids["multi"] = new_e.canonical_entity_id

    # Unresolved disputed
    disputed, _ = founder_add_memory_note(
        db, owner_id=owner_id, content="Maybe switch providers later?", note_type="observation", idempotency_key=f"h-unres-{uuid.uuid4()}"
    )
    disputed.status = "disputed"
    db.flush()
    result.note_ids["unresolved"] = disputed.id

    return result


def answer_history_quality_queries(db: Session, *, owner_id: uuid.UUID) -> HistoryQueryAnswer:
    notes = list(db.execute(select(FounderMemoryNote).where(FounderMemoryNote.owner_id == owner_id)).scalars().all())
    entities = list(db.execute(select(ProjectEntity).where(ProjectEntity.owner_id == owner_id)).scalars().all())
    candidates = list(db.execute(select(WorkCandidate).where(WorkCandidate.owner_id == owner_id)).scalars().all())

    current = [n.content for n in notes if n.status == "active"]
    superseded = [n.content for n in notes if n.status == "superseded"]
    unresolved = [n.content for n in notes if n.status == "disputed"]

    # Duplicates: multiple entity titles that normalize to same fingerprint via reused IDs or aliases
    by_norm: dict[str, list[str]] = {}
    for e in entities:
        key = (e.title_normalized or e.title or "").strip().lower()
        by_norm.setdefault(key, []).append(e.title)
    duplicate = [titles[0] for titles in by_norm.values() if len(titles) > 1]

    # Also treat SAME-collapse (single entity, multiple promotions) as duplicate evidence via aliases count
    from app.models.project_entities import ProjectEntityAlias

    aliases = list(db.execute(select(ProjectEntityAlias).where(ProjectEntityAlias.owner_id == owner_id)).scalars().all())
    if aliases:
        duplicate.extend([f"alias:{a.raw_text}" for a in aliases[:10]])

    affected_work = [f"{c.status}:{c.title}" for c in candidates]
    recap = build_recap(db, owner_id=owner_id, window=RecapWindow.YEAR, include_project_wide=False)
    recent_changes = [f"{i.kind}:{i.title[:80]}" for i in recap.items[:30]]

    evolution = []
    for n in notes:
        if n.supersedes_note_id:
            evolution.append(f"{n.supersedes_note_id} -> {n.id}: {n.content[:80]}")

    return HistoryQueryAnswer(
        current=current,
        superseded=superseded,
        duplicate=duplicate,
        unresolved=unresolved,
        affected_work=affected_work,
        recent_changes=recent_changes,
        historical_evolution=evolution,
    )
