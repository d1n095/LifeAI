"""Inspectable memory foundation — projection + truth-claim receipts.

Does NOT create a second canonical memory store. Assembles a founder-facing lens over
existing tables and records SAID/STORED/… claims so they can be checked against durable
reality. Memory mutation here never grants execution authority.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.founder_memory import (
    list_founder_memory,
    mark_founder_memory_disputed,
    record_founder_memory,
)
from app.founder_memory_signals.service import list_candidate_signals
from app.models.candidate_learning_signal import CandidateLearningSignal
from app.models.founder_memory import FounderMemoryNote
from app.models.mainai_execution import EngineeringLesson, MainAITask
from app.models.memory_truth_claim import (
    MEMORY_TRUTH_STATES,
    MEMORY_TRUTH_TARGET_KINDS,
    MemoryTruthClaim,
)
from app.models.project_entities import ProjectEntity
from app.models.work_candidate import WorkCandidate
from app.work_candidates.service import list_work_candidates


class MemoryTruthState(str, Enum):
    SAID = "said"
    STORED = "stored"
    PLANNED = "planned"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"


class InspectableMemoryError(ValueError):
    pass


@dataclass
class InspectableMemoryItem:
    """Read-only projection — not a table."""

    id: uuid.UUID
    kind: str
    raw_statement: str | None
    normalized_interpretation: str
    related_entities: list[uuid.UUID] = field(default_factory=list)
    confidence: float | None = None
    factual_status: str = "active"
    truth_state: MemoryTruthState = MemoryTruthState.STORED
    plan_references: list[uuid.UUID] = field(default_factory=list)
    task_references: list[uuid.UUID] = field(default_factory=list)
    dependencies: list[uuid.UUID] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    created_at: datetime | None = None
    superseded_by: uuid.UUID | None = None
    corrections: list[uuid.UUID] = field(default_factory=list)
    implementation_status: str | None = None
    verification_status: str | None = None


def _confidence_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    mapping = {"high": 0.9, "medium": 0.6, "low": 0.3, "unknown": None, "confirmed": 1.0, "likely": 0.7, "possible": 0.4}
    if isinstance(value, str):
        return mapping.get(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _from_note(note: FounderMemoryNote, *, corrections: list[uuid.UUID] | None = None) -> InspectableMemoryItem:
    return InspectableMemoryItem(
        id=note.id,
        kind="founder_memory_note",
        raw_statement=note.source,
        normalized_interpretation=note.content,
        confidence=_confidence_float(note.confidence),
        factual_status=note.status,
        truth_state=MemoryTruthState.STORED,
        provenance=dict(note.provenance or {}),
        created_at=note.created_at,
        superseded_by=None,
        corrections=list(corrections or []),
    )


def _from_signal(signal: CandidateLearningSignal) -> InspectableMemoryItem:
    status_map = {"unreviewed": "active", "promoted": "active", "dismissed": "dismissed"}
    return InspectableMemoryItem(
        id=signal.id,
        kind="candidate_learning_signal",
        raw_statement=None,
        normalized_interpretation=signal.classifier_reasoning or f"signal:{signal.signal_kind}",
        confidence=_confidence_float(signal.classifier_confidence),
        factual_status=status_map.get(signal.status, signal.status),
        truth_state=MemoryTruthState.SAID,
        provenance=dict(signal.provenance or {}),
        created_at=signal.created_at,
    )


def _from_work_candidate(candidate: WorkCandidate) -> InspectableMemoryItem:
    if candidate.status == "authorized" and candidate.authorized_goal_id is not None:
        truth = MemoryTruthState.PLANNED
        factual = "active"
    elif candidate.status == "dismissed":
        truth = MemoryTruthState.STORED
        factual = "dismissed"
    else:
        truth = MemoryTruthState.STORED
        factual = "active"
    deps: list[uuid.UUID] = []
    for raw in candidate.dependencies or []:
        try:
            deps.append(uuid.UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    return InspectableMemoryItem(
        id=candidate.id,
        kind="work_candidate",
        raw_statement=candidate.rationale,
        normalized_interpretation=candidate.title,
        related_entities=[candidate.source_entity_id],
        confidence=_confidence_float(candidate.classifier_confidence),
        factual_status=factual,
        truth_state=truth,
        plan_references=[candidate.authorized_goal_id] if candidate.authorized_goal_id else [],
        dependencies=deps,
        provenance=dict(candidate.provenance or {}),
        created_at=candidate.created_at,
    )


def _from_lesson(lesson: EngineeringLesson) -> InspectableMemoryItem:
    factual = "superseded" if lesson.superseded_by is not None else "active"
    truth = MemoryTruthState.VERIFIED if lesson.verification_status == "verified_by_regression_test" else MemoryTruthState.STORED
    return InspectableMemoryItem(
        id=lesson.id,
        kind="engineering_lesson",
        raw_statement=lesson.evidence,
        normalized_interpretation=f"{lesson.problem} → {lesson.fix}",
        confidence=_confidence_float(getattr(lesson.confidence, "value", lesson.confidence)),
        factual_status=factual,
        truth_state=truth,
        provenance={"source_type": lesson.source_type, "source_ref": lesson.source_ref},
        created_at=lesson.created_at,
        superseded_by=lesson.superseded_by,
        verification_status=lesson.verification_status,
    )


def _note_corrections(db: Session, *, owner_id: uuid.UUID, note_id: uuid.UUID) -> list[uuid.UUID]:
    rows = db.execute(
        select(FounderMemoryNote.id).where(
            FounderMemoryNote.owner_id == owner_id,
            FounderMemoryNote.supersedes_note_id == note_id,
        )
    ).scalars().all()
    return list(rows)


def list_inspectable_memory(
    db: Session,
    *,
    owner_id: uuid.UUID,
    kind: str | None = None,
    truth_state: str | None = None,
    factual_status: str | None = None,
    include_lessons: bool = True,
) -> list[InspectableMemoryItem]:
    """Merge projections from existing tables. Never invents authority."""
    items: list[InspectableMemoryItem] = []

    if kind in (None, "founder_memory_note"):
        for note in list_founder_memory(db, owner_id=owner_id):
            items.append(_from_note(note, corrections=_note_corrections(db, owner_id=owner_id, note_id=note.id)))

    if kind in (None, "candidate_learning_signal"):
        for signal in list_candidate_signals(db, owner_id=owner_id):
            items.append(_from_signal(signal))

    if kind in (None, "work_candidate"):
        for candidate in list_work_candidates(db, owner_id=owner_id):
            items.append(_from_work_candidate(candidate))

    if include_lessons and kind in (None, "engineering_lesson"):
        lessons = db.execute(select(EngineeringLesson).order_by(EngineeringLesson.created_at.desc())).scalars().all()
        for lesson in lessons:
            items.append(_from_lesson(lesson))

    if truth_state is not None:
        items = [i for i in items if i.truth_state.value == truth_state]
    if factual_status is not None:
        items = [i for i in items if i.factual_status == factual_status]

    items.sort(key=lambda i: i.created_at or datetime.min, reverse=True)
    return items


def get_inspectable_memory(
    db: Session,
    *,
    owner_id: uuid.UUID,
    item_id: uuid.UUID,
    kind: str | None = None,
) -> InspectableMemoryItem | None:
    for item in list_inspectable_memory(db, owner_id=owner_id, kind=kind):
        if item.id == item_id:
            return item
    return None


def get_inspectable_memory_history(
    db: Session,
    *,
    owner_id: uuid.UUID,
    item_id: uuid.UUID,
) -> list[InspectableMemoryItem]:
    """Walk founder_memory supersession chain oldest-first. Other kinds return the single item."""
    note = db.execute(
        select(FounderMemoryNote).where(FounderMemoryNote.id == item_id, FounderMemoryNote.owner_id == owner_id)
    ).scalar_one_or_none()
    if note is None:
        item = get_inspectable_memory(db, owner_id=owner_id, item_id=item_id)
        return [item] if item is not None else []

    chain: list[FounderMemoryNote] = [note]
    cursor = note
    seen = {note.id}
    while cursor.supersedes_note_id is not None and cursor.supersedes_note_id not in seen:
        parent = db.execute(
            select(FounderMemoryNote).where(
                FounderMemoryNote.id == cursor.supersedes_note_id,
                FounderMemoryNote.owner_id == owner_id,
            )
        ).scalar_one_or_none()
        if parent is None:
            break
        chain.append(parent)
        seen.add(parent.id)
        cursor = parent
    chain.reverse()
    return [_from_note(n, corrections=_note_corrections(db, owner_id=owner_id, note_id=n.id)) for n in chain]


def record_truth_claim(
    db: Session,
    *,
    owner_id: uuid.UUID,
    claim_text: str,
    claimed_state: str,
    target_kind: str,
    idempotency_key: str,
    target_id: uuid.UUID | None = None,
    provenance: dict | None = None,
    verify_now: bool = True,
) -> MemoryTruthClaim:
    """Record a receipt for a memory/work claim. Optionally verify against durable state now.

    Calling convention: build claim_text from the returned durable row's own attributes, never
    from the request alone. This function does not grant authority.
    """
    text = (claim_text or "").strip()
    if not text:
        raise InspectableMemoryError("claim_text must be non-empty")
    if claimed_state not in MEMORY_TRUTH_STATES:
        raise InspectableMemoryError(f"unsupported claimed_state: {claimed_state}")
    if target_kind not in MEMORY_TRUTH_TARGET_KINDS:
        raise InspectableMemoryError(f"unsupported target_kind: {target_kind}")
    if claimed_state != MemoryTruthState.SAID.value and target_id is None:
        raise InspectableMemoryError("target_id is required unless claimed_state is 'said'")

    existing = db.execute(
        select(MemoryTruthClaim).where(
            MemoryTruthClaim.owner_id == owner_id,
            MemoryTruthClaim.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.claim_text != text
            or existing.claimed_state != claimed_state
            or existing.target_kind != target_kind
            or existing.target_id != target_id
        ):
            raise InspectableMemoryError("idempotency_key reused with different claim fields")
        return existing

    row = MemoryTruthClaim(
        owner_id=owner_id,
        claim_text=text,
        claimed_state=claimed_state,
        target_kind=target_kind,
        target_id=target_id,
        provenance=dict(provenance or {}),
        idempotency_key=idempotency_key,
    )
    db.add(row)
    db.flush()

    if verify_now:
        verify_truth_claim(db, owner_id=owner_id, claim_id=row.id)
        db.refresh(row)
    return row


def _actual_truth_state(db: Session, *, owner_id: uuid.UUID, target_kind: str, target_id: uuid.UUID) -> MemoryTruthState | None:
    if target_kind == "founder_memory_note":
        note = db.execute(
            select(FounderMemoryNote).where(FounderMemoryNote.id == target_id, FounderMemoryNote.owner_id == owner_id)
        ).scalar_one_or_none()
        return MemoryTruthState.STORED if note is not None else None

    if target_kind == "candidate_learning_signal":
        signal = db.execute(
            select(CandidateLearningSignal).where(
                CandidateLearningSignal.id == target_id,
                CandidateLearningSignal.owner_id == owner_id,
            )
        ).scalar_one_or_none()
        return MemoryTruthState.SAID if signal is not None else None

    if target_kind == "work_candidate":
        candidate = db.execute(
            select(WorkCandidate).where(WorkCandidate.id == target_id, WorkCandidate.owner_id == owner_id)
        ).scalar_one_or_none()
        if candidate is None:
            return None
        if candidate.status == "authorized" and candidate.authorized_goal_id is not None:
            return MemoryTruthState.PLANNED
        return MemoryTruthState.STORED

    if target_kind == "project_entity":
        entity = db.execute(
            select(ProjectEntity).where(ProjectEntity.id == target_id, ProjectEntity.owner_id == owner_id)
        ).scalar_one_or_none()
        return MemoryTruthState.STORED if entity is not None else None

    if target_kind == "engineering_lesson":
        lesson = db.get(EngineeringLesson, target_id)
        if lesson is None:
            return None
        if lesson.verification_status == "verified_by_regression_test":
            return MemoryTruthState.VERIFIED
        return MemoryTruthState.STORED

    if target_kind == "mainai_task":
        task = db.execute(
            select(MainAITask).where(MainAITask.id == target_id, MainAITask.owner_id == owner_id)
        ).scalar_one_or_none()
        if task is None:
            return None
        status = getattr(task.status, "value", task.status)
        if status == "completed":
            return MemoryTruthState.IMPLEMENTED
        return MemoryTruthState.PLANNED

    if target_kind == "mainai_goal":
        from app.models.mainai_execution import MainAIGoal

        goal = db.execute(
            select(MainAIGoal).where(MainAIGoal.id == target_id, MainAIGoal.owner_id == owner_id)
        ).scalar_one_or_none()
        return MemoryTruthState.PLANNED if goal is not None else None

    return None


def verify_truth_claim(db: Session, *, owner_id: uuid.UUID, claim_id: uuid.UUID) -> MemoryTruthClaim:
    """Fresh-check claimed_state against durable target. False results are kept inspectable."""
    claim = db.execute(
        select(MemoryTruthClaim).where(MemoryTruthClaim.id == claim_id, MemoryTruthClaim.owner_id == owner_id)
    ).scalar_one_or_none()
    if claim is None:
        raise InspectableMemoryError("memory truth claim not found")

    if claim.claimed_state == MemoryTruthState.SAID.value and claim.target_id is None:
        # SAID with no target: claim is about raw expression existence only — true by construction
        # of this receipt existing. Callers that point at a message should pass target_id.
        claim.verified_result = True
    elif claim.target_id is None:
        claim.verified_result = False
    else:
        actual = _actual_truth_state(db, owner_id=owner_id, target_kind=claim.target_kind, target_id=claim.target_id)
        if actual is None:
            claim.verified_result = False
        else:
            # Monotonic: actual must be at least the claimed state in the chain.
            order = [s.value for s in MemoryTruthState]
            claim.verified_result = order.index(actual.value) >= order.index(claim.claimed_state)

    claim.verified_at = datetime.now(timezone.utc)
    db.flush()
    return claim


def list_truth_claim_violations(db: Session, *, owner_id: uuid.UUID) -> list[MemoryTruthClaim]:
    return list(
        db.execute(
            select(MemoryTruthClaim)
            .where(MemoryTruthClaim.owner_id == owner_id, MemoryTruthClaim.verified_result.is_(False))
            .order_by(MemoryTruthClaim.created_at.desc())
        ).scalars().all()
    )


def founder_add_memory_note(
    db: Session,
    *,
    owner_id: uuid.UUID,
    content: str,
    note_type: str,
    idempotency_key: str,
    authority: str = "founder",
    basis: str = "manual",
    source: str | None = None,
    provenance: dict | None = None,
    link_to_work: bool = True,
) -> tuple[FounderMemoryNote, MemoryTruthClaim]:
    """Founder ADD path — delegates to record_founder_memory, then records a STORED claim from the returned row.

    link_to_work=True (default) runs Stage C park linkage. Tests that call
    apply_memory_work_linkage explicitly may pass False to avoid double-apply.
    """
    note = record_founder_memory(
        db,
        owner_id=owner_id,
        note_type=note_type,
        content=content,
        idempotency_key=idempotency_key,
        authority=authority,
        basis=basis,
        source=source,
        provenance=provenance,
    )
    claim = record_truth_claim(
        db,
        owner_id=owner_id,
        claim_text=f"stored founder_memory_note:{note.id} content={note.content[:120]}",
        claimed_state=MemoryTruthState.STORED.value,
        target_kind="founder_memory_note",
        target_id=note.id,
        idempotency_key=f"claim:{idempotency_key}",
        provenance={"via": "founder_add_memory_note"},
        verify_now=True,
    )
    if link_to_work:
        _link_memory_to_work(
            db,
            owner_id=owner_id,
            note_id=note.id,
            note_type=note_type,
            is_correction=False,
        )
    return note, claim


def _link_memory_to_work(
    db: Session,
    *,
    owner_id: uuid.UUID,
    note_id: uuid.UUID,
    note_type: str,
    is_correction: bool,
) -> None:
    """Best-effort Stage C park path — STORED note is never rolled back on linkage failure.

    MEMORY != AUTHORITY. park_candidate=True only; never insert_subordinate / authorize.
    """
    try:
        from app.memory_work_linkage import TimingClass, apply_memory_work_linkage

        timing = (
            TimingClass.NOW
            if note_type in {"correction", "decision", "goal"} or is_correction
            else TimingClass.LATER
        )
        apply_memory_work_linkage(
            db,
            owner_id=owner_id,
            note_id=note_id,
            timing=timing,
            is_correction=is_correction or note_type == "correction",
            park_candidate=True,
            insert_subordinate=False,
        )
    except Exception:
        logger.exception(
            "memory_work_linkage failed after inspectable memory write; note remains stored"
        )


def founder_correct_memory_note(
    db: Session,
    *,
    owner_id: uuid.UUID,
    note_id: uuid.UUID,
    content: str,
    idempotency_key: str,
    note_type: str | None = None,
    link_to_work: bool = True,
) -> tuple[FounderMemoryNote, MemoryTruthClaim]:
    existing = db.execute(
        select(FounderMemoryNote).where(FounderMemoryNote.id == note_id, FounderMemoryNote.owner_id == owner_id)
    ).scalar_one_or_none()
    if existing is None:
        raise InspectableMemoryError("founder memory note not found")
    note = record_founder_memory(
        db,
        owner_id=owner_id,
        note_type=note_type or existing.note_type,
        content=content,
        idempotency_key=idempotency_key,
        authority="founder",
        basis="manual",
        supersedes_note_id=note_id,
        source=f"correction_of:{note_id}",
        provenance={"corrects": str(note_id)},
    )
    claim = record_truth_claim(
        db,
        owner_id=owner_id,
        claim_text=f"stored correction note:{note.id} supersedes:{note_id}",
        claimed_state=MemoryTruthState.STORED.value,
        target_kind="founder_memory_note",
        target_id=note.id,
        idempotency_key=f"claim:{idempotency_key}",
        provenance={"via": "founder_correct_memory_note", "supersedes": str(note_id)},
        verify_now=True,
    )
    if link_to_work:
        _link_memory_to_work(
            db,
            owner_id=owner_id,
            note_id=note.id,
            note_type=note.note_type,
            is_correction=True,
        )
    return note, claim


def founder_dispute_memory_item(
    db: Session,
    *,
    owner_id: uuid.UUID,
    item_id: uuid.UUID,
    kind: str,
    reason: str | None = None,
) -> InspectableMemoryItem:
    from app.founder_memory import FounderMemoryError
    from app.work_candidates.service import WorkCandidateError, dismiss_work_candidate

    try:
        if kind == "founder_memory_note":
            mark_founder_memory_disputed(db, owner_id=owner_id, note_id=item_id)
        elif kind == "work_candidate":
            dismiss_work_candidate(db, owner_id=owner_id, candidate_id=item_id, reason=reason or "founder_dispute")
        else:
            raise InspectableMemoryError(f"dispute not supported for kind={kind}")
    except (FounderMemoryError, WorkCandidateError) as exc:
        raise InspectableMemoryError(str(exc)) from exc
    item = get_inspectable_memory(db, owner_id=owner_id, item_id=item_id, kind=kind)
    if item is None:
        raise InspectableMemoryError("item not found after dispute")
    return item
