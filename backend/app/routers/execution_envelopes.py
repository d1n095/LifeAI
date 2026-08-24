"""Founder-only production API making the proposed-scope -> authorized-envelope edge
reachable: `execution_scope_proposals` -> [founder review/authorization] ->
`execution_authorization_envelopes`. See migration 0057's own module docstring for the full
architecture (`docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md`).

Everything upstream of this edge is already live/automatic: `app.work_candidates.service.
authorize_work_candidate()` (itself reachable via `app/routers/project_entities.py`) already
proposes an execution scope as its own side effect for `task_reference`/`decision`-derived
work candidates. This router closes the remaining founder-governed step -- without it, a
proposal could never actually become real execution authority outside a test.

SECURITY: `owner_id`/`authorized_by` are NEVER accepted from the request body. Every route is
gated by `Depends(require_founder)`, exactly like `app/routers/project_entities.py`/
`app/routers/mainai_execution.py` already establish -- `authorized_by` is always the hardcoded
`"founder"`. `authorized_paths`/`authorized_capabilities`/`authorized_risk` ARE accepted from
the request body -- unlike identity fields, these are the founder's own content-level
decision (accept the proposal as-is, narrow it, or explicitly expand it), the exact judgment
call this whole foundation exists to require a real human act for, not an identity a client
could spoof."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.execution_envelopes import (
    ExecutionEnvelopeError,
    authorize_execution_scope,
    get_current_execution_envelope,
    get_execution_authorization_envelope,
    get_execution_scope_proposal,
    list_execution_authorization_envelopes,
    list_execution_scope_proposals,
    reject_execution_scope,
)
from app.models.user import User
from app.schemas import (
    AuthorizeExecutionScopeIn,
    ExecutionAuthorizationEnvelopeOut,
    ExecutionScopeProposalOut,
    RejectExecutionScopeIn,
)

router = APIRouter(prefix="/api/execution-envelopes", tags=["execution-envelopes"], dependencies=[Depends(require_founder)])


@router.get("/proposals", response_model=list[ExecutionScopeProposalOut])
def list_execution_scope_proposals_route(status_filter: str | None = None, goal_id: uuid.UUID | None = None, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return list_execution_scope_proposals(db, owner_id=user.id, status=status_filter, goal_id=goal_id)


@router.get("/proposals/{proposal_id}", response_model=ExecutionScopeProposalOut)
def get_execution_scope_proposal_route(proposal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    proposal = get_execution_scope_proposal(db, owner_id=user.id, proposal_id=proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution scope proposal not found.")
    return proposal


@router.post("/proposals/{proposal_id}/authorize", response_model=ExecutionAuthorizationEnvelopeOut, status_code=status.HTTP_201_CREATED)
def authorize_execution_scope_route(proposal_id: uuid.UUID, payload: AuthorizeExecutionScopeIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        _, envelope = authorize_execution_scope(
            db, owner_id=user.id, proposal_id=proposal_id, authorized_by="founder",
            authorized_paths=payload.authorized_paths, authorized_capabilities=payload.authorized_capabilities,
            authorized_risk=payload.authorized_risk, envelope_idempotency_key=f"api-authorization:{proposal_id}",
        )
    except ExecutionEnvelopeError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return envelope


@router.post("/proposals/{proposal_id}/reject", response_model=ExecutionScopeProposalOut)
def reject_execution_scope_route(proposal_id: uuid.UUID, payload: RejectExecutionScopeIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        proposal = reject_execution_scope(db, owner_id=user.id, proposal_id=proposal_id, reason=payload.reason)
    except ExecutionEnvelopeError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return proposal


@router.get("/current", response_model=ExecutionAuthorizationEnvelopeOut | None)
def get_current_execution_envelope_route(goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return get_current_execution_envelope(db, owner_id=user.id, goal_id=goal_id)


@router.get("/history", response_model=list[ExecutionAuthorizationEnvelopeOut])
def list_execution_authorization_envelopes_route(goal_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    return list_execution_authorization_envelopes(db, owner_id=user.id, goal_id=goal_id)


@router.get("/{envelope_id}", response_model=ExecutionAuthorizationEnvelopeOut)
def get_execution_authorization_envelope_route(envelope_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    envelope = get_execution_authorization_envelope(db, owner_id=user.id, envelope_id=envelope_id)
    if envelope is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution authorization envelope not found.")
    return envelope
