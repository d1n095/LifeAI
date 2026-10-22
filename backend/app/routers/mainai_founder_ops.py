"""Founder-only HTTP for application-readiness ops.

Exposes STOP, readiness, spend and canonical work status that already exist as Python
services. Does not enable Recall, GitHub writes, activation mode, or first-cycle GO.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_founder
from app.jobs.heartbeat import worker_process_alive
from app.limiter import limiter
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot, recall_http_mounted
from app.models.user import User
from app.routers import health as health_router
from app.workforce.kill_switch import KillSwitchError, activate_kill_switch, query_stop_status

settings = get_settings()

router = APIRouter(
    prefix="/api/mainai/founder-ops",
    tags=["mainai-founder-ops"],
    dependencies=[Depends(require_founder)],
)


class ActivateStopIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _health_payload(db: Session) -> dict:
    healthy = health_router._check_database(db) and health_router._check_redis()
    if not healthy:
        return {"status": "unavailable", "worker": "unknown"}
    worker = "alive" if worker_process_alive() is True else "unknown"
    return {"status": "ok", "worker": worker}


class GitHubIntakeRefreshIn(BaseModel):
    pr_numbers: list[int] = Field(default_factory=list, max_length=20)


@router.post("/github-intake/refresh")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def refresh_github_intake(
    request: Request,
    payload: GitHubIntakeRefreshIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Read-only GitHub intake. Never writes to GitHub or claims agent delivery."""
    from app.agent_coordination.durable import load_owner_ledger, persist_to_assignment
    from app.agent_coordination.github_intake import DEFAULT_PUBLIC_REPO, GitHubDevelopmentIntake, GitHubReadClient
    from app.models.agent_coordination import AgentWorkAssignment
    from sqlalchemy import select

    settings_now = get_settings()
    repo = settings_now.github_repo or DEFAULT_PUBLIC_REPO
    client = GitHubReadClient(repo=repo, token=None)
    service = load_owner_ledger(db, owner_id=user.id)
    intake = GitHubDevelopmentIntake(service, owner_id=str(user.id), repo=repo)
    result = intake.refresh(client, numbers=payload.pr_numbers or None)
    assignment = db.execute(
        select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == user.id)
    ).scalars().first()
    if assignment is not None:
        obligation_id = next(iter(service._obligations), None)
        if obligation_id:
            persist_to_assignment(db, assignment=assignment, service=service, obligation_id=obligation_id)
            db.commit()
    result["write_attempted"] = False
    result["github_write"] = False
    return result


@router.get("/snapshot")
def founder_ops_snapshot(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    return build_founder_ops_snapshot(
        db,
        owner_id=user.id,
        health=_health_payload(db),
        recall_http_mounted=recall_http_mounted(request.app),
    )


@router.get("/stop")
def get_stop_status(db: Session = Depends(get_db), user: User = Depends(require_founder)):
    try:
        payload = query_stop_status(db, owner_id=user.id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="STOP-status kunde inte läsas. Behandla som blockerad.",
        ) from exc
    payload["observability_failed"] = False
    return payload


@router.post("/stop")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def activate_stop(
    request: Request,
    payload: ActivateStopIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    try:
        state = activate_kill_switch(db, owner_id=user.id, reason=payload.reason)
        db.commit()
    except KillSwitchError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"stop": state.as_dict(), "query": query_stop_status(db, owner_id=user.id)}
