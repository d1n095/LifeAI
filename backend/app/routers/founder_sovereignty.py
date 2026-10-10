"""Remote Founder approval control plane.

Consumable later from phone, tablet, desktop, or watch. No device UI in this lane.
Founder-only. Does not grant merge, deploy, or Recall authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.mainai_founder_sovereignty.service import (
    approval_context,
    decide_approval,
    inspect_active_policies,
    issue_founder_step_up,
    list_pending_approvals,
    restore_founder_policy_to_version,
    rollback_founder_policy,
)
from app.mainai_founder_sovereignty.types import ActorKind, ApprovalMode, SovereigntyError, StepUpPurpose
from app.models.user import User

router = APIRouter(prefix="/api/founder-sovereignty", tags=["founder-sovereignty"], dependencies=[Depends(require_founder)])


class DecideIn(BaseModel):
    mode: ApprovalMode
    expected_snapshot_hash: str
    duration_seconds: int | None = None
    until: datetime | None = None
    limits: dict | None = None


class RollbackIn(BaseModel):
    policy_key: str
    reason: str


class RestoreIn(BaseModel):
    policy_key: str
    target_version: int
    reason: str


class StepUpIn(BaseModel):
    purpose: StepUpPurpose
    password: str


@router.get("/approvals")
def pending_approvals(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    requests = list_pending_approvals(db, actor_id=user.id)
    return [
        {
            "id": str(item.id),
            "who": str(item.principal_id),
            "wants": item.capability_key,
            "resource": item.resource,
            "action": item.action,
            "data": item.requested_data,
            "duration": item.requested_duration,
            "consequences": item.consequences,
            "risk_tier": item.risk_tier,
            "snapshot_hash": item.snapshot_hash,
            "context": approval_context(item, who_label=str(item.principal_id)).__dict__,
        }
        for item in requests
    ]


@router.post("/approvals/{request_id}/decide")
def decide(request_id: UUID, payload: DecideIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        receipt = decide_approval(
            db,
            actor_id=user.id,
            request_id=request_id,
            mode=payload.mode,
            duration=timedelta(seconds=payload.duration_seconds) if payload.duration_seconds else None,
            until=payload.until,
            limits=payload.limits,
            expected_snapshot_hash=payload.expected_snapshot_hash,
        )
        db.commit()
    except SovereigntyError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"receipt_id": str(receipt.id), "decision": receipt.decision}


@router.get("/policies")
def policies(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return [
        {
            "policy_key": item.policy_key,
            "policy_class": item.policy_class.value,
            "version": item.version_number,
            "payload": item.payload,
            "reason": item.reason,
            "previous_version": item.previous_version_number,
        }
        for item in inspect_active_policies(db, actor_id=user.id)
    ]


@router.post("/policies/rollback")
def rollback(payload: RollbackIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        version = rollback_founder_policy(
            db,
            actor_id=user.id,
            actor_kind=ActorKind.FOUNDER,
            policy_key=payload.policy_key,
            reason=payload.reason,
        )
        db.commit()
    except SovereigntyError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"policy_key": version.policy_key, "version": version.version_number}


@router.post("/policies/restore")
def restore(payload: RestoreIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        version = restore_founder_policy_to_version(
            db,
            actor_id=user.id,
            actor_kind=ActorKind.FOUNDER,
            policy_key=payload.policy_key,
            target_version=payload.target_version,
            reason=payload.reason,
        )
        db.commit()
    except SovereigntyError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"policy_key": version.policy_key, "version": version.version_number}


@router.post("/step-up")
def step_up(payload: StepUpIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        receipt = issue_founder_step_up(db, purpose=payload.purpose, password=payload.password)
        db.commit()
    except SovereigntyError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return {"step_up_id": str(receipt.id), "purpose": receipt.purpose, "expires_at": receipt.expires_at.isoformat()}
