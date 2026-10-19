"""Founder-only HTTP for application-readiness ops.

Exposes STOP, readiness, spend and canonical work status that already exist as Python
services. Does not enable Recall, GitHub writes, activation mode, or first-cycle GO.
"""

from __future__ import annotations

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
