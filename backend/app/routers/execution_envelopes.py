"""Founder-only production API making the proposed-scope -> authorized-envelope edge
reachable: `execution_scope_proposals` -> [founder review/authorization] ->
`execution_authorization_envelopes`. See migration 0057's own module docstring for the full
architecture (`docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md`).

Path A (a goal originating from a `WorkCandidate` -> `authorize_work_candidate()`, itself
reachable via `app/routers/project_entities.py`) already proposes an execution scope
automatically, as a side effect of founder work-candidate authorization -- no separate action
needed there. Path B (a goal created directly via `POST /api/mainai/goals`) previously had NO
route to ever create a proposal at all -- found this session (`docs/MAINAI_V1_GOAL_TO_
AUTONOMY.md`'s gap #0): a directly-created goal could be planned/decomposed but could never
reach execution authority through any real API interaction. `propose_execution_scope_route`
below closes that gap -- an explicit, founder-invoked action (deliberately NOT automatic the
way Path A's trigger is, since Path B has no upstream founder-authorization act like
`authorize_work_candidate()` to piggyback on).

SECURITY: `owner_id`/`authorized_by` are NEVER accepted from the request body. Every route is
gated by `Depends(require_founder)`, exactly like `app/routers/project_entities.py`/
`app/routers/mainai_execution.py` already establish -- `authorized_by` is always the hardcoded
`"founder"`. `authorized_paths`/`authorized_capabilities`/`authorized_risk` ARE accepted from
the request body -- unlike identity fields, these are the founder's own content-level
decision (accept the proposal as-is, narrow it, or explicitly expand it), the exact judgment
call this whole foundation exists to require a real human act for, not an identity a client
could spoof. `propose_execution_scope_route`'s `proposed_*` fields are likewise the founder's
OWN stated content, carrying zero authority either way -- see `propose_execution_scope()`'s
own docstring: PROPOSED_SCOPE != AUTHORIZED_SCOPE, structurally, regardless of who or what
proposed it."""

import hashlib
import json
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
    propose_execution_scope,
    reject_execution_scope,
)
from app.models.user import User
from app.schemas import (
    AuthorizeExecutionScopeIn,
    ExecutionAuthorizationEnvelopeOut,
    ExecutionScopeProposalOut,
    ProposeExecutionScopeIn,
    RejectExecutionScopeIn,
)

router = APIRouter(prefix="/api/execution-envelopes", tags=["execution-envelopes"], dependencies=[Depends(require_founder)])


@router.post("/goals/{goal_id}/propose", response_model=ExecutionScopeProposalOut, status_code=status.HTTP_201_CREATED)
def propose_execution_scope_route(goal_id: uuid.UUID, payload: ProposeExecutionScopeIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Path B bridge: the founder-invoked route that lets a directly-created goal
    (`POST /api/mainai/goals`) reach the same proposal -> authorization -> envelope chain
    Path A gets automatically. Deliberately explicit, not automatic -- Path B has no
    upstream founder-authorization act to piggyback on the way Path A's `authorize_work_
    candidate()` does. Carries zero authority either way: `authorize_execution_scope_route`
    below still requires its own separate, explicit founder call with its own explicit
    authorized_paths/authorized_capabilities before any real execution authority exists.

    idempotency_key is a stable hash of (goal_id, proposed content) -- not a random value --
    so an accidental retry with the IDENTICAL payload is a true no-op (returns the same
    existing row), while a deliberate new proposal with DIFFERENT content (e.g. after the
    founder rejected an earlier one and wants to try again) is never blocked by history."""
    digest = hashlib.sha256(
        json.dumps(
            {
                "goal_id": str(goal_id),
                "proposed_paths": sorted(payload.proposed_paths),
                "proposed_capabilities": sorted(payload.proposed_capabilities),
                "proposed_risk": payload.proposed_risk,
                "repository_identity": payload.repository_identity,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    try:
        proposal = propose_execution_scope(
            db, owner_id=user.id, goal_id=goal_id,
            idempotency_key=f"api-direct-goal-proposal:{goal_id}:{digest}",
            repository_identity=payload.repository_identity,
            proposed_paths=payload.proposed_paths,
            proposed_capabilities=payload.proposed_capabilities,
            proposed_risk=payload.proposed_risk,
            proposal_reasoning="Founder-submitted proposal for a directly-created goal.",
            proposal_strategy="api_direct_goal_proposal_v1",
            provenance={"source": "propose_execution_scope_route", "goal_id": str(goal_id)},
        )
    except ExecutionEnvelopeError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return proposal


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
