"""Stage F — continuous simplification proposals (never auto-removes authority/security)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.concept_reconciliation.normalize import jaccard, normalize_concept_text, token_set
from app.founder_memory import list_founder_memory
from app.models.project_entities import ProjectEntity
from app.work_candidates import list_work_candidates

# Hard protected domains — proposals may FLAG complexity here but must never recommend removal.
PROTECTED_DOMAINS = frozenset({
    "security",
    "authority",
    "audit",
    "recovery",
    "rls",
    "erasure",
    "lease",
    "execution_envelope",
})


class SimplificationKind(str, Enum):
    DUPLICATE_CONCEPT = "duplicate_concept"
    DUPLICATE_WORKFLOW = "duplicate_workflow"
    ORPHAN_MEMORY = "orphan_memory"
    STALE_SUMMARY = "stale_summary"
    REPEATED_MANUAL_TRANSLATION = "repeated_manual_translation"
    UNNECESSARY_TASK_COMPLEXITY = "unnecessary_task_complexity"
    TEMPORARY_ARCHITECTURE = "temporary_architecture"


@dataclass
class SimplificationProposal:
    kind: SimplificationKind
    title: str
    rationale: str
    evidence_refs: list[dict] = field(default_factory=list)
    protected: bool = False
    auto_apply_allowed: bool = False  # Stage F never auto-applies


@dataclass
class SimplificationReport:
    owner_id: uuid.UUID
    proposals: list[SimplificationProposal] = field(default_factory=list)
    protected_domains: tuple[str, ...] = tuple(sorted(PROTECTED_DOMAINS))


def _touches_protected(text: str) -> bool:
    norm = normalize_concept_text(text)
    return any(p in norm for p in PROTECTED_DOMAINS)


def propose_duplicate_concepts(db: Session, *, owner_id: uuid.UUID) -> list[SimplificationProposal]:
    entities = list(
        db.execute(
            select(ProjectEntity).where(
                ProjectEntity.owner_id == owner_id,
                ProjectEntity.status.in_(("active", "proposed")),
            )
        ).scalars().all()
    )
    proposals: list[SimplificationProposal] = []
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for i, a in enumerate(entities):
        ta = token_set(a.title or "")
        for b in entities[i + 1 :]:
            if a.entity_type != b.entity_type:
                continue
            score = jaccard(ta, token_set(b.title or ""))
            if score < 0.6 and normalize_concept_text(a.title) != normalize_concept_text(b.title):
                continue
            pair = tuple(sorted((a.id, b.id)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            text = f"{a.title} / {b.title}"
            proposals.append(
                SimplificationProposal(
                    kind=SimplificationKind.DUPLICATE_CONCEPT,
                    title=f"Collapse near-duplicate concepts: {a.title[:80]}",
                    rationale=f"Jaccard={score:.2f}; consider alias/SAME-collapse rather than two live entities.",
                    evidence_refs=[{"kind": "project_entity", "id": str(a.id)}, {"kind": "project_entity", "id": str(b.id)}],
                    protected=_touches_protected(text),
                )
            )
    return proposals


def propose_duplicate_workflows(db: Session, *, owner_id: uuid.UUID) -> list[SimplificationProposal]:
    candidates = [c for c in list_work_candidates(db, owner_id=owner_id) if c.status == "unreviewed"]
    proposals: list[SimplificationProposal] = []
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for i, a in enumerate(candidates):
        ta = token_set(a.title or "")
        for b in candidates[i + 1 :]:
            score = jaccard(ta, token_set(b.title or ""))
            if score < 0.8:
                continue
            pair = tuple(sorted((a.id, b.id)))
            if pair in seen:
                continue
            seen.add(pair)
            proposals.append(
                SimplificationProposal(
                    kind=SimplificationKind.DUPLICATE_WORKFLOW,
                    title=f"Duplicate unreviewed work candidates: {(a.title or '')[:80]}",
                    rationale="Near-identical unreviewed candidates — review for collapse/supersede.",
                    evidence_refs=[{"kind": "work_candidate", "id": str(a.id)}, {"kind": "work_candidate", "id": str(b.id)}],
                    protected=_touches_protected(f"{a.title} {b.title}"),
                )
            )
    return proposals


def propose_orphan_memory(db: Session, *, owner_id: uuid.UUID) -> list[SimplificationProposal]:
    from app.models.memory_thread import MemoryThreadMember

    notes = list_founder_memory(db, owner_id=owner_id, status="active")
    proposals: list[SimplificationProposal] = []
    for note in notes:
        linked = db.execute(
            select(MemoryThreadMember.id).where(
                MemoryThreadMember.owner_id == owner_id,
                MemoryThreadMember.member_kind == "founder_memory_note",
                MemoryThreadMember.member_ref_id == str(note.id),
            ).limit(1)
        ).scalar_one_or_none()
        if linked is not None:
            continue
        proposals.append(
            SimplificationProposal(
                kind=SimplificationKind.ORPHAN_MEMORY,
                title=f"Orphan founder memory: {(note.content or '')[:80]}",
                rationale="Active note has no memory-thread membership — consider linkage or park.",
                evidence_refs=[{"kind": "founder_memory_note", "id": str(note.id)}],
                protected=_touches_protected(note.content or ""),
            )
        )
    return proposals


def propose_temporary_architecture_markers(db: Session, *, owner_id: uuid.UUID) -> list[SimplificationProposal]:
    markers = ("tmp", "temporary", "hack", "workaround", "wip", "throwaway")
    proposals: list[SimplificationProposal] = []
    entities = db.execute(
        select(ProjectEntity).where(ProjectEntity.owner_id == owner_id, ProjectEntity.status.in_(("active", "proposed")))
    ).scalars().all()
    for entity in entities:
        title = (entity.title or "").lower()
        if not any(m in title for m in markers):
            continue
        proposals.append(
            SimplificationProposal(
                kind=SimplificationKind.TEMPORARY_ARCHITECTURE,
                title=f"Temporary architecture marker: {entity.title[:80]}",
                rationale="Title suggests temporary/workaround architecture — schedule retirement review.",
                evidence_refs=[{"kind": "project_entity", "id": str(entity.id)}],
                protected=_touches_protected(entity.title or ""),
            )
        )
    return proposals


def propose_simplifications(db: Session, *, owner_id: uuid.UUID) -> SimplificationReport:
    """Detect measurable simplification opportunities. Never auto-applies. Never recommends
    removing security/authority/audit/recovery surfaces — those proposals are flagged protected.
    """
    proposals: list[SimplificationProposal] = []
    proposals.extend(propose_duplicate_concepts(db, owner_id=owner_id))
    proposals.extend(propose_duplicate_workflows(db, owner_id=owner_id))
    proposals.extend(propose_orphan_memory(db, owner_id=owner_id))
    proposals.extend(propose_temporary_architecture_markers(db, owner_id=owner_id))
    # Enforce: protected proposals never claim auto_apply_allowed.
    for p in proposals:
        if p.protected:
            p.auto_apply_allowed = False
    return SimplificationReport(owner_id=owner_id, proposals=proposals)
