"""Founder-only activation authority APIs; no endpoint performs a remote mutation."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.integrations.github_client import GitHubClient
from app.mainai_execution.activation_security import (
    ActivationAuthorityError,
    AuthoritativePreflightFacts,
    cancel_push_grant,
    create_preflight_receipt,
    create_push_grant,
)
from app.models.user import User
from app.schemas import ActivationPreflightIn, ActivationPreflightOut, PushGrantCreateIn, PushGrantOut

router = APIRouter(prefix="/api/mainai/execution", tags=["mainai-activation-security"])


def _deny(exc: ActivationAuthorityError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc), "paid_replan_allowed": False})


@router.post("/goals/{goal_id}/preflight", response_model=ActivationPreflightOut, status_code=status.HTTP_201_CREATED)
async def activation_preflight(goal_id: uuid.UUID, payload: ActivationPreflightIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    settings = get_settings()
    observed_base_sha = None
    try:
        client = GitHubClient()
        if client.is_configured() and settings.github_repo:
            observed_base_sha = await client.get_ref(payload.base_branch)
    except Exception:
        # An unreadable required fact is NO-GO, never a 500 or optimistic assumption.
        observed_base_sha = None
    runtime = {
        "github_repo": settings.github_repo,
        "github_write_enabled": settings.github_write_enabled,
        "github_auto_merge_enabled": settings.github_auto_merge_enabled,
        "legacy_prepare_pr_writes_enabled": settings.mainai_legacy_prepare_pr_writes_enabled,
        "base_branch": settings.github_worktree_base_branch,
        "pr_base_branch": settings.github_pr_base_branch,
    }
    facts = AuthoritativePreflightFacts(
        activation_sha=settings.mainai_activation_sha or None,
        repository_identity=settings.github_repo,
        integration_branch=settings.mainai_activation_integration_branch or None,
        observed_base_sha=observed_base_sha,
        runtime_config=runtime,
        runtime_token_identity=settings.mainai_activation_runtime_token_identity or None,
        runtime_token_attestation_digest=settings.mainai_activation_token_attestation_digest or None,
        ruleset_attestation_digest=settings.mainai_activation_ruleset_attestation_digest or None,
        founder_attestation_digest=settings.mainai_activation_founder_attestation_digest or None,
        negative_authority_confirmed=(not settings.github_auto_merge_enabled and not settings.mainai_legacy_prepare_pr_writes_enabled),
    )
    try:
        row = create_preflight_receipt(
            db, owner_id=user.id, goal_id=goal_id,
            execution_envelope_id=payload.execution_envelope_id,
            provider_spend_authorization_id=payload.provider_spend_authorization_id,
            expected_repository_identity=payload.repository_identity,
            expected_integration_branch=payload.integration_branch,
            expected_base_branch=payload.base_branch, expected_base_sha=payload.base_sha,
            expected_activation_sha=payload.activation_sha,
            expected_authority_version=payload.authority_version,
            facts=facts, expires_at=payload.expires_at, founder_authorized=True,
        )
        db.commit()
        return row
    except ActivationAuthorityError as exc:
        db.rollback()
        raise _deny(exc) from exc


@router.post("/tasks/{task_id}/push-grants", response_model=PushGrantOut, status_code=status.HTTP_201_CREATED)
def create_push_grant_route(task_id: uuid.UUID, payload: PushGrantCreateIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        row = create_push_grant(db, owner_id=user.id, task_id=task_id, authorized_by=user.email,
            founder_authorized=True, **payload.model_dump())
        db.commit()
        return row
    except ActivationAuthorityError as exc:
        db.rollback()
        raise _deny(exc) from exc


@router.post("/push-grants/{grant_id}/cancel", response_model=PushGrantOut)
def cancel_push_grant_route(grant_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        row = cancel_push_grant(db, owner_id=user.id, grant_id=grant_id, founder_authorized=True)
        db.commit()
        return row
    except ActivationAuthorityError as exc:
        db.rollback()
        raise _deny(exc) from exc
