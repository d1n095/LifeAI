"""Founder-only production API making the two governed manual edges in the closing-phase
cognition chain reachable:

    interpretation_proposal -> [founder review/promotion]  -> ProjectEntity
    WorkCandidate            -> [founder authorization]     -> MainAIGoal

Everything upstream of these two edges is already live/automatic: claim extraction ->
candidate interpretation proposal (app/rag/claims.py), and ProjectEntity promotion ->
candidate work candidate (app/project_entities/service.py's own SAVEPOINT-wired side effect).
This router closes the remaining gap -- without it, a real production request could never
actually reach `promote_interpretation_proposal()`/`authorize_work_candidate()`, only a test
calling the service function directly could. Same "component tested" != "runtime reachable"
distinction this mission's own review discipline established throughout.

Reuses the existing services (`app.project_entities`, `app.work_candidates`) exactly as they
are -- no duplicated state machine, no second promotion/authorization path.

SECURITY: `owner_id`/`authority`/`authorized_by` are NEVER accepted from the request body.
Every route is gated by `Depends(require_founder)`, which verifies both `role == founder` AND
`id == FOUNDER_USER_ID` (the one fixed row `app/bootstrap.py` provisions, see `app/deps.py`'s
own docstring) -- so `owner_id` is always `user.id` from that dependency, `authority`/`basis`
on a promoted `ProjectEntity` are always the hardcoded `"founder"`/`"manual"` (this specific
API IS the founder personally reviewing and approving; a client cannot submit an arbitrary
`authority` value and have it trusted), and `authorized_by` on `authorize_work_candidate()` is
always the hardcoded `"founder"`, matching `app/routers/mainai_execution.py`'s own
`create_goal(..., created_by="founder")` precedent exactly. This is the SAME discipline
`promote_interpretation_proposal()`/`authorize_work_candidate()` themselves already enforce at
the service layer (explicit, non-defaulted `authority`/`basis`/`authorized_by`) -- this router
does not weaken it, it is simply the one place in the whole system allowed to supply
`"founder"` as those values, because it is the one place that has actually verified the caller
IS the founder."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.user import User
from app.project_entities import (
    ProjectEntityError,
    dismiss_interpretation_proposal,
    get_interpretation_proposal,
    get_project_entity,
    list_current_project_entities,
    list_interpretation_proposals,
    promote_interpretation_proposal,
)
from app.schemas import (
    AuthorizeWorkCandidateIn,
    DismissInterpretationProposalIn,
    DismissWorkCandidateIn,
    InterpretationProposalOut,
    ProjectEntityOut,
    PromoteInterpretationProposalIn,
    WorkCandidateOut,
)
from app.work_candidates import (
    WorkCandidateError,
    authorize_work_candidate,
    dismiss_work_candidate,
    get_work_candidate,
    list_work_candidates,
)

router = APIRouter(prefix="/api/project-entities", tags=["project-entities"], dependencies=[Depends(require_founder)])


@router.get("/interpretation-proposals", response_model=list[InterpretationProposalOut])
def list_interpretation_proposals_route(status_filter: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return list_interpretation_proposals(db, owner_id=user.id, status=status_filter)


@router.get("/interpretation-proposals/{proposal_id}", response_model=InterpretationProposalOut)
def get_interpretation_proposal_route(proposal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    proposal = get_interpretation_proposal(db, owner_id=user.id, proposal_id=proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interpretation proposal not found.")
    return proposal


@router.post("/interpretation-proposals/{proposal_id}/promote", response_model=ProjectEntityOut, status_code=status.HTTP_201_CREATED)
def promote_interpretation_proposal_route(proposal_id: uuid.UUID, payload: PromoteInterpretationProposalIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        _, entity = promote_interpretation_proposal(
            db, owner_id=user.id, proposal_id=proposal_id, entity_type=payload.entity_type, title=payload.title,
            summary=payload.summary, confidence=payload.confidence, decided_by=payload.decided_by,
            decided_at=payload.decided_at, supersedes_entity_id=payload.supersedes_entity_id,
            authority="founder", basis="manual", entity_idempotency_key=f"api-promotion:{proposal_id}",
        )
    except ProjectEntityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return entity


@router.post("/interpretation-proposals/{proposal_id}/dismiss", response_model=InterpretationProposalOut)
def dismiss_interpretation_proposal_route(proposal_id: uuid.UUID, payload: DismissInterpretationProposalIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        proposal = dismiss_interpretation_proposal(db, owner_id=user.id, proposal_id=proposal_id, reason=payload.reason)
    except ProjectEntityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return proposal


@router.get("/entities", response_model=list[ProjectEntityOut])
def list_project_entities_route(entity_type: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return list_current_project_entities(db, owner_id=user.id, entity_type=entity_type)


@router.get("/entities/{entity_id}", response_model=ProjectEntityOut)
def get_project_entity_route(entity_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    entity = get_project_entity(db, owner_id=user.id, entity_id=entity_id)
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project entity not found.")
    return entity


@router.get("/work-candidates", response_model=list[WorkCandidateOut])
def list_work_candidates_route(status_filter: str | None = None, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return list_work_candidates(db, owner_id=user.id, status=status_filter)


@router.get("/work-candidates/{candidate_id}", response_model=WorkCandidateOut)
def get_work_candidate_route(candidate_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    candidate = get_work_candidate(db, owner_id=user.id, candidate_id=candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work candidate not found.")
    return candidate


@router.post("/work-candidates/{candidate_id}/authorize", status_code=status.HTTP_201_CREATED)
def authorize_work_candidate_route(candidate_id: uuid.UUID, payload: AuthorizeWorkCandidateIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    from app.models.mainai_execution import MainAIGoalRiskLevel
    from app.schemas import MainAIGoalOut

    try:
        risk_level = MainAIGoalRiskLevel(payload.risk_level)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid risk_level '{payload.risk_level}'.") from exc

    try:
        _, goal = authorize_work_candidate(
            db, owner_id=user.id, candidate_id=candidate_id, authorized_by="founder",
            title=payload.title, risk_level=risk_level, approval_policy=payload.approval_policy,
        )
    except WorkCandidateError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return MainAIGoalOut.model_validate(goal)


@router.post("/work-candidates/{candidate_id}/dismiss", response_model=WorkCandidateOut)
def dismiss_work_candidate_route(candidate_id: uuid.UUID, payload: DismissWorkCandidateIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        candidate = dismiss_work_candidate(db, owner_id=user.id, candidate_id=candidate_id, reason=payload.reason)
    except WorkCandidateError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return candidate
