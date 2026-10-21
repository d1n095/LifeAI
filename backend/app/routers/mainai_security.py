"""Founder-only HTTP for security diagnostics. Never applies sensitive changes."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.mainai_founder_ops.snapshot import recall_http_mounted
from app.mainai_security.dashboard import build_security_dashboard
from app.mainai_security.incidents import acknowledge_incident, preserve_incident
from app.mainai_security.remediation import (
    SensitiveChangeRefused,
    approve_remediation,
    propose_remediation,
    refuse_apply,
)
from app.mainai_security.restrictions import explain_restrictions
from app.models.user import User

settings = get_settings()

router = APIRouter(
    prefix="/api/mainai/security",
    tags=["mainai-security"],
    dependencies=[Depends(require_founder)],
)


class ProposeIn(BaseModel):
    finding_id: str = Field(min_length=1, max_length=128)
    change_class: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    isolated_test_plan: str = Field(default="isolated checkout tests only", max_length=500)


class IncidentIn(BaseModel):
    kind: str = Field(min_length=1, max_length=128)
    confidence: str = Field(min_length=1, max_length=32)
    severity: str = Field(min_length=1, max_length=32)
    evidence: dict = Field(default_factory=dict)


class ApplyIn(BaseModel):
    github_write_enabled: bool | None = None
    mainai_activation_mode: bool | None = None
    secret_key: str | None = None
    policy: dict | None = None


@router.get("/dashboard")
def security_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    return build_security_dashboard(
        db,
        owner_id=user.id,
        recall_http_mounted=recall_http_mounted(request.app),
    )


@router.get("/restrictions")
def security_restrictions(
    request: Request,
    capability: str | None = None,
    user: User = Depends(require_founder),
):
    return {
        "invented": False,
        "first_cycle_go": False,
        "restrictions": explain_restrictions(
            recall_http_mounted=recall_http_mounted(request.app),
            requested=capability,
        ),
    }


@router.post("/remediation/propose")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def security_propose(
    request: Request,
    payload: ProposeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    return propose_remediation(
        db,
        owner_id=user.id,
        finding_id=payload.finding_id,
        change_class=payload.change_class,
        title=payload.title,
        summary=payload.summary,
        isolated_test_plan=payload.isolated_test_plan,
    )


@router.post("/remediation/{proposal_id}/approve")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def security_approve(
    request: Request,
    proposal_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        return approve_remediation(db, owner_id=user.id, proposal_id=proposal_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found.") from exc


@router.post("/remediation/{proposal_id}/apply")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def security_apply(
    request: Request,
    proposal_id: str,
    payload: ApplyIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    if payload and any(
        value is not None
        for value in (
            payload.github_write_enabled,
            payload.mainai_activation_mode,
            payload.secret_key,
            payload.policy,
        )
    ):
        reason = "Policy or credential mutation is refused. Founder approval does not apply settings from this module."
    else:
        reason = "Security remediations are never applied autonomously. Approval records intent only."
    try:
        refuse_apply(db, owner_id=user.id, proposal_id=proposal_id, reason=reason)
    except SensitiveChangeRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/incidents/preserve")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def security_preserve_incident(
    request: Request,
    payload: IncidentIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    return preserve_incident(
        db,
        owner_id=user.id,
        incident=payload.model_dump(),
    )


@router.post("/incidents/{incident_id}/acknowledge")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def security_acknowledge_incident(
    request: Request,
    incident_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    return acknowledge_incident(db, owner_id=user.id, incident_id=incident_id)
