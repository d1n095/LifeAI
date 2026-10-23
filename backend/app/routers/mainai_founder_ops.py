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


class CoordinationEventIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=128)
    agent_identity: str | None = None
    assignment_id: str | None = None
    observation: dict | None = None
    evidence: dict | None = None
    to: str | None = None
    from_github_ci: bool = False


class IssueBridgeIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=32)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


@router.post("/github-intake/refresh")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def refresh_github_intake(
    request: Request,
    payload: GitHubIntakeRefreshIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Read-only GitHub intake. Never writes to GitHub or claims agent delivery."""
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.github_intake import DEFAULT_PUBLIC_REPO, GitHubDevelopmentIntake, GitHubReadClient

    from app.agent_coordination.persist import persist_owner_ledger

    settings_now = get_settings()
    repo = settings_now.github_repo or DEFAULT_PUBLIC_REPO
    client = GitHubReadClient(repo=repo, token=None)
    service = load_owner_ledger(db, owner_id=user.id)
    intake = GitHubDevelopmentIntake(service, owner_id=str(user.id), repo=repo)
    result = intake.refresh(client, numbers=payload.pr_numbers or None)
    persist_owner_ledger(db, owner_id=user.id, service=service, actor="founder")
    db.commit()
    result["write_attempted"] = False
    result["github_write"] = False
    result["persisted_without_manual_assignment"] = True
    return result


@router.post("/coordination/events")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def ingest_coordination_event(
    request: Request,
    payload: CoordinationEventIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Authenticated lifecycle ingestion. Never writes to GitHub or claims vendor delivery."""
    from app.agent_coordination.lifecycle import ingest_lifecycle_event

    body = payload.model_dump()
    result = ingest_lifecycle_event(db, owner_id=user.id, payload=body, actor="founder")
    db.commit()
    result["first_cycle_go"] = False
    result["delivery_claimed"] = False
    result["github_write"] = False
    return result


@router.post("/agent-gateway/bridges")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def issue_agent_bridge(
    request: Request,
    payload: IssueBridgeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Issue a scoped local-bridge token. Not a Cursor/Claude/Codex vendor credential."""
    from app.agent_coordination.delivery import issue_bridge_credential

    result = issue_bridge_credential(
        db,
        owner_id=user.id,
        recipient=payload.recipient,
        actor="founder",
        ttl_seconds=payload.ttl_seconds,
    )
    if result.get("outcome") != "issued":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.get("reason"))
    db.commit()
    result["github_write"] = False
    result["first_cycle_go"] = False
    return result


@router.get("/agent-gateway/setup")
def agent_gateway_setup(user: User = Depends(require_founder)):
    """Named missing capabilities and Founder setup only. Never a vendor credential."""
    from app.agent_coordination.connect import founder_setup_requirements

    return {
        "requirements": founder_setup_requirements(),
        "vendor_delivery_claimed": False,
        "first_cycle_go": False,
        "github_write": False,
    }


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
