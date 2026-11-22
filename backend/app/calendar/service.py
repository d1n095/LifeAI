"""Founder calendar domain.

Manual, owner-scoped scheduling only. Recurrence is materialized into a bounded series of rows
sharing a `series_id`, so listing/conflict detection uses the same table and needs no external
calendar provider or background scheduler. No autonomous event creation or mutation exists.
"""

from __future__ import annotations

import calendar as calendar_module
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.calendar import CalendarEvent

MAX_SERIES_OCCURRENCES = 52
DEFAULT_SERIES_DAYS = 180


class CalendarError(ValueError):
    pass


class EventNotFoundError(CalendarError):
    pass


class InvalidEventTimesError(CalendarError):
    pass


class InvalidEventTransitionError(CalendarError):
    pass


class InvalidCalendarFieldError(CalendarError):
    pass


class StaleCalendarEventError(CalendarError):
    pass


_TERMINAL_STATUSES = frozenset({"cancelled", "completed"})
_CATEGORIES = frozenset({"personal", "work", "school"})
_RECURRENCE_RULES = frozenset({"none", "daily", "weekly", "monthly"})


def _event(
    db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID, lock: bool = False
) -> CalendarEvent:
    q = select(CalendarEvent).where(
        CalendarEvent.id == event_id,
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.deleted_at.is_(None),
    )
    row = db.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise EventNotFoundError("calendar event is missing or belongs to another owner")
    return row


def _normalize(value: datetime | None, timezone: str) -> datetime | None:
    if value is None:
        return None
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise InvalidCalendarFieldError(f"Okänd tidszon: {timezone}") from exc
    if value.tzinfo is not None:
        return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    # A naive datetime is already the founder's local wall clock. Preserve the selected zone
    # on the row, but store the same wall-clock value so existing naive-UTC reads stay stable.
    return value


def _normalize_query_bound(value: datetime | None) -> datetime | None:
    """Normalize a query bound to the same naive-UTC representation stored on rows.

    Browser clients send timezone-aware ISO strings (`...Z`), while calendar rows are stored
    naive (see `_normalize`). Comparing the two directly in Python raises TypeError, so every
    range/conflict bound passes through this helper before touching a row.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return value


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar_module.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _next_occurrence(value: datetime, rule: str) -> datetime:
    if rule == "daily":
        return value + timedelta(days=1)
    if rule == "weekly":
        return value + timedelta(days=7)
    if rule == "monthly":
        return _add_months(value, 1)
    raise InvalidCalendarFieldError(f"Okänd upprepning: {rule}")


def _validate_fields(
    *, title: str, starts_at: datetime, ends_at: datetime | None, category: str, recurrence_rule: str
) -> None:
    if not title.strip():
        raise InvalidEventTimesError("title must not be empty")
    if ends_at is not None and ends_at <= starts_at:
        raise InvalidEventTimesError("ends_at must be after starts_at")
    if category not in _CATEGORIES:
        raise InvalidCalendarFieldError("category måste vara personal, work eller school")
    if recurrence_rule not in _RECURRENCE_RULES:
        raise InvalidCalendarFieldError("recurrence_rule måste vara none, daily, weekly eller monthly")


def create_event(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    description: str | None = None,
    location: str | None = None,
    category: str = "personal",
    timezone: str = "UTC",
    recurrence_rule: str = "none",
    recurrence_until: datetime | None = None,
    project_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    milestone_id: uuid.UUID | None = None,
) -> CalendarEvent:
    starts_at = _normalize(starts_at, timezone)
    ends_at = _normalize(ends_at, timezone)
    recurrence_until = _normalize(recurrence_until, timezone)
    _validate_fields(
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        category=category,
        recurrence_rule=recurrence_rule,
    )
    if recurrence_rule == "none":
        recurrence_until = None
    if recurrence_until is not None and recurrence_until < starts_at:
        raise InvalidEventTimesError("recurrence_until must not be before starts_at")

    series_id = uuid.uuid4() if recurrence_rule != "none" else None
    base = CalendarEvent(
        owner_id=owner_id,
        project_id=project_id,
        task_id=task_id,
        milestone_id=milestone_id,
        title=title,
        description=description,
        location=location,
        starts_at=starts_at,
        ends_at=ends_at,
        category=category,
        timezone=timezone,
        recurrence_rule=recurrence_rule,
        recurrence_until=recurrence_until,
        series_id=series_id,
        recurrence_index=0,
    )
    db.add(base)
    if recurrence_rule == "none":
        db.flush()
        return base

    horizon = recurrence_until or (starts_at + timedelta(days=DEFAULT_SERIES_DAYS))
    duration = (ends_at - starts_at) if ends_at is not None else None
    current = starts_at
    index = 0
    while index < MAX_SERIES_OCCURRENCES - 1:
        current = _next_occurrence(current, recurrence_rule)
        if current > horizon:
            break
        index += 1
        db.add(
            CalendarEvent(
                owner_id=owner_id,
                project_id=project_id,
                task_id=task_id,
                milestone_id=milestone_id,
                title=title,
                description=description,
                location=location,
                starts_at=current,
                ends_at=(current + duration) if duration is not None else None,
                category=category,
                timezone=timezone,
                recurrence_rule=recurrence_rule,
                recurrence_until=recurrence_until,
                series_id=series_id,
                recurrence_index=index,
            )
        )
    db.flush()
    return base


def update_event(
    db: Session,
    *,
    owner_id: uuid.UUID,
    event_id: uuid.UUID,
    fields: dict,
    expected_version: int | None = None,
    apply_to_series: bool = False,
) -> CalendarEvent:
    row = _event(db, owner_id=owner_id, event_id=event_id, lock=True)
    if expected_version is not None and expected_version != (row.version or 1):
        raise StaleCalendarEventError("Händelsen har ändrats sedan den lästes. Läs om och försök igen.")
    if row.status in _TERMINAL_STATUSES:
        raise InvalidEventTransitionError(f"event is already '{row.status}', cannot edit")

    title = fields.get("title", row.title)
    timezone = fields.get("timezone", row.timezone)
    starts_at = _normalize(fields.get("starts_at", row.starts_at), timezone)
    ends_at = _normalize(fields.get("ends_at", row.ends_at), timezone)
    category = fields.get("category", row.category)
    _validate_fields(
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        category=category,
        recurrence_rule=row.recurrence_rule,
    )

    rows = [row]
    if apply_to_series and row.series_id:
        rows = (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.series_id == row.series_id,
                CalendarEvent.owner_id == owner_id,
                CalendarEvent.deleted_at.is_(None),
                CalendarEvent.status == "scheduled",
            )
            .all()
        )
    start_delta = starts_at - row.starts_at
    end_delta = (ends_at - row.ends_at) if ends_at is not None and row.ends_at is not None else None
    for item in rows:
        item.title = title
        item.description = fields.get("description", item.description)
        item.location = fields.get("location", item.location)
        item.category = category
        item.timezone = timezone
        item.project_id = fields.get("project_id", item.project_id)
        item.task_id = fields.get("task_id", item.task_id)
        item.milestone_id = fields.get("milestone_id", item.milestone_id)
        if "starts_at" in fields:
            item.starts_at = item.starts_at + start_delta
        if "ends_at" in fields:
            if end_delta is not None:
                item.ends_at = (item.ends_at + end_delta) if item.ends_at else None
            else:
                item.ends_at = fields.get("ends_at")
        item.version = (item.version or 1) + 1
        item.updated_at = datetime.utcnow()  # noqa: DTZ003 - existing naive UTC storage convention
    db.flush()
    return row


def delete_event(
    db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID, apply_to_series: bool = False
) -> None:
    row = _event(db, owner_id=owner_id, event_id=event_id, lock=True)
    rows = [row]
    if apply_to_series and row.series_id:
        rows = (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.series_id == row.series_id,
                CalendarEvent.owner_id == owner_id,
                CalendarEvent.deleted_at.is_(None),
            )
            .all()
        )
    now = datetime.utcnow()  # noqa: DTZ003 - existing naive UTC storage convention
    for item in rows:
        item.deleted_at = now
        item.updated_at = now
        item.version = (item.version or 1) + 1
    db.flush()


def cancel_event(
    db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID, reason: str | None = None
) -> CalendarEvent:
    row = _event(db, owner_id=owner_id, event_id=event_id, lock=True)
    if row.status in _TERMINAL_STATUSES:
        raise InvalidEventTransitionError(f"event is already '{row.status}', cannot cancel")
    row.status = "cancelled"
    row.cancelled_at = datetime.utcnow()  # noqa: DTZ003 - existing naive UTC storage convention
    row.cancellation_reason = reason
    row.updated_at = datetime.utcnow()  # noqa: DTZ003 - existing naive UTC storage convention
    row.version = (row.version or 1) + 1
    db.flush()
    return row


def complete_event(db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID) -> CalendarEvent:
    row = _event(db, owner_id=owner_id, event_id=event_id, lock=True)
    if row.status in _TERMINAL_STATUSES:
        raise InvalidEventTransitionError(f"event is already '{row.status}', cannot complete")
    row.status = "completed"
    row.updated_at = datetime.utcnow()  # noqa: DTZ003 - existing naive UTC storage convention
    row.version = (row.version or 1) + 1
    db.flush()
    return row


def get_event(db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID) -> CalendarEvent:
    return _event(db, owner_id=owner_id, event_id=event_id)


def list_in_range(
    db: Session,
    *,
    owner_id: uuid.UUID,
    starts_after: datetime,
    starts_before: datetime,
    category: str | None = None,
    project_id: uuid.UUID | None = None,
) -> list[CalendarEvent]:
    starts_after = _normalize_query_bound(starts_after)
    starts_before = _normalize_query_bound(starts_before)
    q = select(CalendarEvent).where(
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.starts_at >= starts_after,
        CalendarEvent.starts_at < starts_before,
    )
    if category is not None:
        q = q.where(CalendarEvent.category == category)
    if project_id is not None:
        q = q.where(CalendarEvent.project_id == project_id)
    rows = db.execute(
        q.order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
    ).scalars().all()
    return list(rows)


def list_conflicts(
    db: Session,
    *,
    owner_id: uuid.UUID,
    starts_at: datetime,
    ends_at: datetime | None = None,
    exclude_event_id: uuid.UUID | None = None,
) -> list[CalendarEvent]:
    starts_at = _normalize_query_bound(starts_at)
    ends_at = _normalize_query_bound(ends_at)
    end = ends_at or (starts_at + timedelta(hours=1))
    if end <= starts_at:
        raise InvalidEventTimesError("ends_at must be after starts_at")
    candidates = (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.owner_id == owner_id,
            CalendarEvent.deleted_at.is_(None),
            CalendarEvent.status == "scheduled",
            CalendarEvent.starts_at < end,
            or_(
                CalendarEvent.ends_at.is_(None),
                CalendarEvent.ends_at > starts_at,
            ),
        )
        .order_by(CalendarEvent.starts_at.asc(), CalendarEvent.id.asc())
        .all()
    )
    conflicts: list[CalendarEvent] = []
    for item in candidates:
        if exclude_event_id is not None and item.id == exclude_event_id:
            continue
        item_end = item.ends_at or (item.starts_at + timedelta(hours=1))
        if item_end > starts_at and item.starts_at < end:
            conflicts.append(item)
    return conflicts


def list_upcoming(
    db: Session, *, owner_id: uuid.UUID, within: timedelta | None = None, limit: int = 50
) -> list[CalendarEvent]:
    now = datetime.utcnow()  # noqa: DTZ003 - existing naive UTC storage convention
    q = select(CalendarEvent).where(
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.status == "scheduled",
        CalendarEvent.starts_at >= now,
    )
    if within is not None:
        q = q.where(CalendarEvent.starts_at < now + within)
    rows = db.execute(q.order_by(CalendarEvent.starts_at).limit(limit)).scalars().all()
    return list(rows)


@dataclass(frozen=True)
class UpcomingEventSummary:
    id: uuid.UUID
    title: str
    starts_at: datetime
    ends_at: datetime | None
    location: str | None


def upcoming_summaries(
    db: Session, *, owner_id: uuid.UUID, within: timedelta, limit: int = 20
) -> list[UpcomingEventSummary]:
    return [
        UpcomingEventSummary(id=row.id, title=row.title, starts_at=row.starts_at, ends_at=row.ends_at, location=row.location)
        for row in list_upcoming(db, owner_id=owner_id, within=within, limit=limit)
    ]
