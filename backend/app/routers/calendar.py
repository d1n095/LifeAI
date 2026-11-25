"""Founder calendar API.

`owner_id` is never accepted from a request body. Every route is founder-gated and all writes
are owner-scoped. External calendar sync and autonomous changes are deliberately absent.
"""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.calendar import (
    CalendarError,
    EventNotFoundError,
    InvalidCalendarFieldError,
    InvalidEventTimesError,
    StaleCalendarEventError,
    cancel_event,
    complete_event,
    create_event,
    delete_event,
    get_event,
    list_conflicts,
    list_in_range,
    list_upcoming,
    update_event,
)
from app.db import get_db
from app.deps import require_founder
from app.models.calendar import CalendarEvent
from app.models.project import Project, Task
from app.models.user import User
from app.models.work_management import WorkspaceMilestone
from app.schemas import (
    CalendarEventOut,
    CancelCalendarEventIn,
    CreateCalendarEventIn,
    UpdateCalendarEventIn,
)

router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(require_founder)])


def _owned_link_checks(
    db: Session,
    *,
    user: User,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    milestone_id: uuid.UUID | None,
) -> None:
    if project_id is not None:
        project = (
            db.query(Project)
            .filter(Project.id == project_id, Project.created_by == user.id, Project.deleted_at.is_(None))
            .first()
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Projektet hittades inte.")
    if task_id is not None:
        task = (
            db.query(Task)
            .filter(Task.id == task_id, Task.created_by == user.id, Task.deleted_at.is_(None))
            .first()
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Uppgiften hittades inte.")
    if milestone_id is not None:
        milestone = (
            db.query(WorkspaceMilestone)
            .filter(
                WorkspaceMilestone.id == milestone_id,
                WorkspaceMilestone.owner_id == user.id,
            )
            .first()
        )
        if milestone is None:
            raise HTTPException(status_code=404, detail="Milstolpen hittades inte.")


def _calendar_error(exc: Exception) -> HTTPException:
    if isinstance(exc, EventNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaleCalendarEventError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (InvalidEventTimesError, InvalidCalendarFieldError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post("/events", response_model=CalendarEventOut, status_code=status.HTTP_201_CREATED)
def create_calendar_event(
    body: CreateCalendarEventIn,
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> CalendarEventOut:
    _owned_link_checks(
        db,
        user=user,
        project_id=body.project_id,
        task_id=body.task_id,
        milestone_id=body.milestone_id,
    )
    try:
        row = create_event(
            db,
            owner_id=user.id,
            title=body.title,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            description=body.description,
            location=body.location,
            category=body.category,
            timezone=body.timezone,
            recurrence_rule=body.recurrence_rule,
            recurrence_until=body.recurrence_until,
            project_id=body.project_id,
            task_id=body.task_id,
            milestone_id=body.milestone_id,
        )
        db.commit()
    except CalendarError as exc:
        db.rollback()
        raise _calendar_error(exc) from exc
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
    category: str | None = Query(default=None),
    project_id: uuid.UUID | None = Query(default=None),
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> list[CalendarEventOut]:
    rows = list_in_range(
        db,
        owner_id=user.id,
        starts_after=starts_after,
        starts_before=starts_before,
        category=category,
        project_id=project_id,
    )
    return [CalendarEventOut.model_validate(row) for row in rows]


@router.get("/conflicts", response_model=list[CalendarEventOut])
def get_calendar_conflicts(
    starts_at: datetime,
    ends_at: datetime | None = None,
    exclude_event_id: uuid.UUID | None = None,
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> list[CalendarEventOut]:
    try:
        rows = list_conflicts(
            db,
            owner_id=user.id,
            starts_at=starts_at,
            ends_at=ends_at,
            exclude_event_id=exclude_event_id,
        )
    except CalendarError as exc:
        raise _calendar_error(exc) from exc
    return [CalendarEventOut.model_validate(row) for row in rows]


@router.get("/events/{event_id}", response_model=CalendarEventOut)
def get_calendar_event(
    event_id: uuid.UUID, user: User = Depends(require_founder), db: Session = Depends(get_db)
) -> CalendarEventOut:
    try:
        row = get_event(db, owner_id=user.id, event_id=event_id)
    except EventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CalendarEventOut.model_validate(row)


@router.patch("/events/{event_id}", response_model=CalendarEventOut)
def update_calendar_event(
    event_id: uuid.UUID,
    body: UpdateCalendarEventIn,
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
) -> CalendarEventOut:
    fields = body.model_dump(exclude_unset=True)
    expected_version = fields.pop("expected_version", None)
    apply_to_series = bool(fields.pop("apply_to_series", False))
    if any(key in fields for key in ("project_id", "task_id", "milestone_id")):
        _owned_link_checks(
            db,
            user=user,
            project_id=fields.get("project_id"),
            task_id=fields.get("task_id"),
            milestone_id=fields.get("milestone_id"),
        )
    try:
        row = update_event(
            db,
            owner_id=user.id,
            event_id=event_id,
            fields=fields,
            expected_version=expected_version,
            apply_to_series=apply_to_series,
        )
        db.commit()
    except CalendarError as exc:
        db.rollback()
        raise _calendar_error(exc) from exc
    return CalendarEventOut.model_validate(row)


@router.delete("/events/{event_id}")
def delete_calendar_event(
    event_id: uuid.UUID,
    apply_to_series: bool = Query(default=False),
    user: User = Depends(require_founder),
    db: Session = Depends(get_db),
):
    try:
        delete_event(db, owner_id=user.id, event_id=event_id, apply_to_series=apply_to_series)
        db.commit()
    except CalendarError as exc:
        db.rollback()
        raise _calendar_error(exc) from exc
    return {"status": "deleted"}


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
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CalendarError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CalendarError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CalendarEventOut.model_validate(row)
