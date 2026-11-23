"""Round 10 M7: the improvement-proposal router — founder-gated initiative queue.

POST /api/mainai/improvement-proposals records a proposal (MainAI recognizing an
opportunity). The response carries execution_attempted=False STRUCTURALLY: there is no
execution path behind this endpoint — recording a proposal is the whole operation. The
founder-only decision endpoints (accept/reject/implement/revert) close the loop with
audit-trail entries. GET lists the queue.

This endpoint set is reachable only with require_founder; the service re-checks the role.
No proposal execution, no privileged code change, no new agent mission, no security-policy
modification is reachable from here — by construction, not by instruction."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.deps import get_db
from app.mainai_execution.improvement_proposals import (
    AlreadyDecided,
    ForbiddenDecision,
    ImprovementProposalError,
    InvalidDecision,
    decide_proposal,
    list_proposals,
    mark_implemented,
    mark_reverted,
    propose_improvement,
)
from app.models.improvement_proposal import ImprovementSource, ImprovementStatus
from app.models.user import User
from app.deps import require_founder

router = APIRouter(prefix="/api/mainai", tags=["mainai-improvement-proposals"])


class ImprovementProposalIn(BaseModel):
    source: str = Field(..., description="recurring_failure_cluster | lesson_recurrence | founder_observation | manual")
    title: str = Field(..., min_length=4, max_length=200)
    rationale: str = Field(..., min_length=8)
    benefit_estimate: str | None = None
    risk_estimate: str | None = None


class ImprovementProposalOut(BaseModel):
    id: uuid.UUID
    source: str
    title: str
    rationale: str
    benefit_estimate: str | None
    risk_estimate: str | None
    status: str
    proposed_by: str
    duplicate: bool
    # STRUCTURAL honesty: the endpoint that creates a proposal executes NOTHING — there
    # is no execution path. This field makes that absence visible in every response.
    execution_attempted: bool = False


def _out(proposal, duplicate: bool = False) -> ImprovementProposalOut:
    return ImprovementProposalOut(
        id=proposal.id, source=proposal.source_type.value, title=proposal.title,
        rationale=proposal.rationale, benefit_estimate=proposal.benefit_estimate,
        risk_estimate=proposal.risk_estimate, status=proposal.status.value,
        proposed_by=proposal.proposed_by, duplicate=duplicate,
    )


@router.post("/improvement-proposals", response_model=ImprovementProposalOut)
def create_improvement_proposal(
    request: Request,
    payload: ImprovementProposalIn,
    db=Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        source = ImprovementSource(payload.source)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"unknown source: {payload.source}")
    proposal, duplicate = propose_improvement(
        db, owner_id=user.id, source=source, title=payload.title,
        rationale=payload.rationale, benefit_estimate=payload.benefit_estimate,
        risk_estimate=payload.risk_estimate,
    )
    return _out(proposal, duplicate)


@router.get("/improvement-proposals", response_model=list[ImprovementProposalOut])
def list_improvement_proposals(
    status: str | None = None,
    db=Depends(get_db),
    user: User = Depends(require_founder),
):
    status_enum = ImprovementStatus(status) if status else None
    return [_out(p) for p in list_proposals(db, owner_id=user.id, status=status_enum)]


class ImprovementDecisionIn(BaseModel):
    decision: str = Field(..., description="accepted | rejected")
    notes: str = Field(..., min_length=3)


@router.post("/improvement-proposals/{proposal_id}/decision", response_model=ImprovementProposalOut)
def decide_improvement_proposal(
    request: Request,
    proposal_id: uuid.UUID,
    payload: ImprovementDecisionIn,
    db=Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        decision = ImprovementStatus(payload.decision)
        proposal = decide_proposal(db, user=user, proposal_id=proposal_id, decision=decision, notes=payload.notes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ForbiddenDecision as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except AlreadyDecided as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ImprovementProposalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _out(proposal)


class ImplementationIn(BaseModel):
    notes: str = Field(..., min_length=3)


@router.post("/improvement-proposals/{proposal_id}/implemented", response_model=ImprovementProposalOut)
def mark_proposal_implemented(
    request: Request,
    proposal_id: uuid.UUID,
    payload: ImplementationIn,
    db=Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        proposal = mark_implemented(db, user=user, proposal_id=proposal_id, notes=payload.notes)
    except ForbiddenDecision as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except InvalidDecision as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _out(proposal)


@router.post("/improvement-proposals/{proposal_id}/reverted", response_model=ImprovementProposalOut)
def mark_proposal_reverted(
    request: Request,
    proposal_id: uuid.UUID,
    payload: ImplementationIn,
    db=Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        proposal = mark_reverted(db, user=user, proposal_id=proposal_id, notes=payload.notes)
    except ForbiddenDecision as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except InvalidDecision as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _out(proposal)
