"""Founder-only production API for provider-spend grants.

REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY. An execution envelope never implies
spend. This router is the runtime-reachable founder edge that creates/revokes spend
budgets for a specific (goal, current envelope).

SECURITY: `owner_id` / `authorized_by` are NEVER accepted from the request body.
`Depends(require_founder)` gates every route; `authorized_by` is always the hardcoded
`"founder"`.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.models.user import User
from app.provider_spend import (
    ProviderSpendError,
    authorize_provider_spend,
    get_current_provider_spend_authorization,
    revoke_provider_spend,
)

router = APIRouter(
    prefix="/api/provider-spend",
    tags=["provider-spend"],
    dependencies=[Depends(require_founder)],
)


class AuthorizeProviderSpendIn(BaseModel):
    goal_id: uuid.UUID
    execution_envelope_id: uuid.UUID
    max_cost_usd: Decimal
    max_requests: int
    idempotency_key: str = Field(min_length=1, max_length=128)
    max_prompt_tokens: int | None = None
    max_completion_tokens: int | None = None
    max_cost_per_request_usd: Decimal | None = None
    allowed_providers: list[str] = Field(default_factory=list)
    allowed_models: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class RevokeProviderSpendIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ProviderSpendAuthorizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    goal_id: uuid.UUID
    execution_envelope_id: uuid.UUID
    authorized_by: str
    authorized_at: datetime
    status: str
    max_cost_usd: Decimal
    max_requests: int
    max_prompt_tokens: int | None = None
    max_completion_tokens: int | None = None
    max_cost_per_request_usd: Decimal | None = None
    allowed_providers: list
    allowed_models: list
    expires_at: datetime | None = None
    spent_cost_usd: Decimal
    spent_requests: int
    reserved_cost_usd: Decimal
    reserved_requests: int
    created_at: datetime


@router.post("/authorize", response_model=ProviderSpendAuthorizationOut, status_code=status.HTTP_201_CREATED)
def authorize_provider_spend_route(
    payload: AuthorizeProviderSpendIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        row = authorize_provider_spend(
            db,
            owner_id=user.id,
            goal_id=payload.goal_id,
            execution_envelope_id=payload.execution_envelope_id,
            authorized_by="founder",
            max_cost_usd=payload.max_cost_usd,
            max_requests=payload.max_requests,
            idempotency_key=payload.idempotency_key,
            max_prompt_tokens=payload.max_prompt_tokens,
            max_completion_tokens=payload.max_completion_tokens,
            max_cost_per_request_usd=payload.max_cost_per_request_usd,
            allowed_providers=payload.allowed_providers,
            allowed_models=payload.allowed_models,
            expires_at=payload.expires_at,
            provenance={"via": "api/provider-spend/authorize"},
        )
    except ProviderSpendError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return row


@router.post("/{authorization_id}/revoke", response_model=ProviderSpendAuthorizationOut)
def revoke_provider_spend_route(
    authorization_id: uuid.UUID,
    payload: RevokeProviderSpendIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        row = revoke_provider_spend(
            db, owner_id=user.id, authorization_id=authorization_id, reason=payload.reason
        )
    except ProviderSpendError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    return row


@router.get("/current", response_model=ProviderSpendAuthorizationOut | None)
def get_current_provider_spend_route(
    goal_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    return get_current_provider_spend_authorization(db, owner_id=user.id, goal_id=goal_id)
