"""Owner-visible runtime incident API.

No endpoint uploads diagnostics, changes code, restarts a worker or invokes a provider.
Acknowledging/resolving only changes the local incident's status.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.incidents.service import (
    STATUSES,
    acknowledge_incident,
    get_incident,
    list_incidents,
    resolve_incident,
)
from app.models.user import User

router = APIRouter(prefix="/api/incidents", tags=["incidents"], dependencies=[Depends(require_founder)])


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    error_code: str
    component: str
    severity: str
    status: str
    summary: str
    correlation_id: str | None
    app_version: str | None
    occurrence_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    details: dict
    analysis: dict
    recovery_options: list
    acknowledged_at: datetime | None
    resolved_at: datetime | None


@router.get("", response_model=list[IncidentOut])
def list_runtime_incidents(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=422, detail="Okänd incidentstatus.")
    return list_incidents(db, owner_id=user.id, status=status, limit=limit)


@router.get("/{incident_id}", response_model=IncidentOut)
def read_runtime_incident(
    incident_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    row = get_incident(db, owner_id=user.id, incident_id=incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Incidenten hittades inte.")
    return row


@router.post("/{incident_id}/acknowledge", response_model=IncidentOut)
def acknowledge_runtime_incident(
    incident_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    row = acknowledge_incident(db, owner_id=user.id, incident_id=incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Incidenten hittades inte.")
    db.commit()
    db.refresh(row)
    return row


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
def resolve_runtime_incident(
    incident_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    row = resolve_incident(db, owner_id=user.id, incident_id=incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Incidenten hittades inte.")
    db.commit()
    db.refresh(row)
    return row
