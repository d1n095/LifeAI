"""Founder-gated inspectable memory API.

Routes under /api/founder/memory. Write paths only wrap existing record_*/mark_*/dismiss_*
functions — never invent authority or hard-delete.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.inspectable_memory import (
    InspectableMemoryError,
    founder_add_memory_note,
    founder_correct_memory_note,
    founder_dispute_memory_item,
    get_inspectable_memory,
    get_inspectable_memory_history,
    list_inspectable_memory,
    list_truth_claim_violations,
    record_truth_claim,
    verify_truth_claim,
)
from app.inspectable_memory.schemas import (
    FounderAddMemoryIn,
    FounderCorrectMemoryIn,
    FounderDisputeMemoryIn,
    InspectableMemoryItemOut,
    MemoryTruthClaimOut,
    RecordTruthClaimIn,
)
from app.inspectable_memory.service import InspectableMemoryItem
from app.models.memory_truth_claim import MemoryTruthClaim
from app.models.user import User

router = APIRouter(prefix="/api/founder/memory", tags=["founder-memory"], dependencies=[Depends(require_founder)])


def _item_out(item: InspectableMemoryItem) -> InspectableMemoryItemOut:
    return InspectableMemoryItemOut(
        id=item.id,
        kind=item.kind,
        raw_statement=item.raw_statement,
        normalized_interpretation=item.normalized_interpretation,
        related_entities=item.related_entities,
        confidence=item.confidence,
        factual_status=item.factual_status,
        truth_state=item.truth_state.value if hasattr(item.truth_state, "value") else str(item.truth_state),
        plan_references=item.plan_references,
        task_references=item.task_references,
        dependencies=item.dependencies,
        risks=item.risks,
        provenance=item.provenance,
        created_at=item.created_at,
        superseded_by=item.superseded_by,
        corrections=item.corrections,
        implementation_status=item.implementation_status,
        verification_status=item.verification_status,
    )


def _claim_out(claim: MemoryTruthClaim) -> MemoryTruthClaimOut:
    return MemoryTruthClaimOut(
        id=claim.id,
        claim_text=claim.claim_text,
        claimed_state=claim.claimed_state,
        target_kind=claim.target_kind,
        target_id=claim.target_id,
        verified_at=claim.verified_at,
        verified_result=claim.verified_result,
        provenance=dict(claim.provenance or {}),
        created_at=claim.created_at,
        idempotency_key=claim.idempotency_key,
    )


@router.get("", response_model=list[InspectableMemoryItemOut])
def list_memory_route(
    kind: str | None = None,
    truth_state: str | None = None,
    factual_status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    items = list_inspectable_memory(
        db, owner_id=user.id, kind=kind, truth_state=truth_state, factual_status=factual_status
    )
    return [_item_out(i) for i in items]


@router.get("/violations", response_model=list[MemoryTruthClaimOut])
def list_violations_route(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return [_claim_out(c) for c in list_truth_claim_violations(db, owner_id=user.id)]


@router.get("/{item_id}", response_model=InspectableMemoryItemOut)
def get_memory_route(item_id: uuid.UUID, kind: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    item = get_inspectable_memory(db, owner_id=user.id, item_id=item_id, kind=kind)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory item not found")
    return _item_out(item)


@router.get("/{item_id}/history", response_model=list[InspectableMemoryItemOut])
def get_memory_history_route(item_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return [_item_out(i) for i in get_inspectable_memory_history(db, owner_id=user.id, item_id=item_id)]


@router.post("", response_model=InspectableMemoryItemOut, status_code=status.HTTP_201_CREATED)
def add_memory_route(payload: FounderAddMemoryIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        note, _claim = founder_add_memory_note(
            db,
            owner_id=user.id,
            content=payload.content,
            note_type=payload.note_type,
            idempotency_key=payload.idempotency_key,
            source=payload.source,
            provenance=payload.provenance,
        )
        db.commit()
    except InspectableMemoryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    item = get_inspectable_memory(db, owner_id=user.id, item_id=note.id, kind="founder_memory_note")
    assert item is not None
    return _item_out(item)


@router.post("/{item_id}/correct", response_model=InspectableMemoryItemOut)
def correct_memory_route(
    item_id: uuid.UUID,
    payload: FounderCorrectMemoryIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        note, _claim = founder_correct_memory_note(
            db,
            owner_id=user.id,
            note_id=item_id,
            content=payload.content,
            idempotency_key=payload.idempotency_key,
            note_type=payload.note_type,
        )
        db.commit()
    except InspectableMemoryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    item = get_inspectable_memory(db, owner_id=user.id, item_id=note.id, kind="founder_memory_note")
    assert item is not None
    return _item_out(item)


@router.post("/{item_id}/dispute", response_model=InspectableMemoryItemOut)
def dispute_memory_route(
    item_id: uuid.UUID,
    payload: FounderDisputeMemoryIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        item = founder_dispute_memory_item(
            db, owner_id=user.id, item_id=item_id, kind=payload.kind, reason=payload.reason
        )
        db.commit()
    except InspectableMemoryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _item_out(item)


@router.post("/claims", response_model=MemoryTruthClaimOut, status_code=status.HTTP_201_CREATED)
def record_claim_route(payload: RecordTruthClaimIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        claim = record_truth_claim(
            db,
            owner_id=user.id,
            claim_text=payload.claim_text,
            claimed_state=payload.claimed_state,
            target_kind=payload.target_kind,
            target_id=payload.target_id,
            idempotency_key=payload.idempotency_key,
            provenance=payload.provenance,
            verify_now=payload.verify_now,
        )
        db.commit()
    except InspectableMemoryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _claim_out(claim)


@router.post("/claims/{claim_id}/verify", response_model=MemoryTruthClaimOut)
def verify_claim_route(claim_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        claim = verify_truth_claim(db, owner_id=user.id, claim_id=claim_id)
        db.commit()
    except InspectableMemoryError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _claim_out(claim)
