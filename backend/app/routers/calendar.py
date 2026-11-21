"""Founder calendar API -- see app/calendar/service.py for the domain rationale.

SECURITY: `owner_id` is NEVER accepted from the request body -- every route is gated by
`Depends(require_founder)`, matching `app/routers/project_entities.py`'s own precedent exactly,
so `owner_id` is always `user.id` from that dependency.
"""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.calendar import (
    CalendarError,
    EventNotFoundError,
    cancel_event,
    complete_event,
    create_event,
    get_event,
    list_in_range,
    list_upcoming,
)
from app.db import get_db
from app.deps import require_founder
from app.models.user import User
from app.schemas import CalendarEventOut, CancelCalendarEventIn, CreateCalendarEventIn

router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(require_founder)])


@router.post("/events", response_model=CalendarEventOut, status_code=status.HTTP_201_CREATED)
def create_calendar_event(
    body: CreateCalendarEventIn, user: User = Depends(require_founder), db: Session = Depends(get_db)
) -> CalendarEventOut:
    try:
        row = create_event(
            db,
            owner_id=user.id,
            title=body.title,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            description=body.description,
            location=body.location,
        )
        db.commit()
    except CalendarError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return CalendarEventOut.model_validate(row)


@router.get("/events/upcoming", response_model=list[CalendarEventOut])
def get_upcoming_calendar_events(
    within_hours: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> list[CalendarEventOut]:
    within = timedelta(hours=within_hours) if within_hours is not None else None
    rows = list_upcoming(db, owner_id=user.id, within=within, limit=limit)
    return [CalendarEventOut.model_validate(row) for row in rows]


@router.get("/events", response_model=list[CalendarEventOut])
def list_calendar_events_in_range(
    starts_after: datetime,
    starts_before: datetime,
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> list[CalendarEventOut]:
    rows = list_in_range(db, owner_id=user.id, starts_after=starts_after, starts_before=starts_before)
    return [CalendarEventOut.model_validate(row) for row in rows]


@router.get("/events/{event_id}", response_model=CalendarEventOut)
def get_calendar_event(
    event_id: uuid.UUID, user: User = Depends(require_founder), db: Session = Depends(get_db)
) -> CalendarEventOut:
    try:
        row = get_event(db, owner_id=user.id, event_id=event_id)
    except EventNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return CalendarEventOut.model_validate(row)


@router.post("/events/{event_id}/cancel", response_model=CalendarEventOut)
def cancel_calendar_event(
    event_id: uuid.UUID,
    body: CancelCalendarEventIn,
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> CalendarEventOut:
    try:
        row = cancel_event(db, owner_id=user.id, event_id=event_id, reason=body.reason)
        db.commit()
    except EventNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CalendarError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return CalendarEventOut.model_validate(row)


@router.post("/events/{event_id}/complete", response_model=CalendarEventOut)
def complete_calendar_event(
    event_id: uuid.UUID, user: User = Depends(require_founder), db: Session = Depends(get_db)
) -> CalendarEventOut:
    try:
        row = complete_event(db, owner_id=user.id, event_id=event_id)
        db.commit()
    except EventNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CalendarError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return CalendarEventOut.model_validate(row)
