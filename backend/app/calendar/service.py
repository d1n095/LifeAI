"""Founder calendar foundation -- ordinary owner-scoped scheduling data.

First increment of the founder directive's Priority 3 ("Context-aware calendar, task
management and proactive assistance"). Deliberately narrow: manual events only, no external
provider sync (Google Calendar/Outlook) -- that is a real external-integration decision
(provider choice, credential/OAuth model, sync-conflict semantics) outside this task's
authority to decide unilaterally, left for a later, separate task.

`list_upcoming()` is the "proactive assistance" read path: callers (e.g. a future session-
context assembler, matching the existing `app.active_context`-style consumption pattern used
throughout this codebase) can surface what's coming up without any new cross-module coupling
-- this module only reads/writes its own table.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.calendar import CalendarEvent


class CalendarError(ValueError):
    pass


class EventNotFoundError(CalendarError):
    pass


class InvalidEventTimesError(CalendarError):
    pass


class InvalidEventTransitionError(CalendarError):
    pass


_TERMINAL_STATUSES = frozenset({"cancelled", "completed"})


def _event(db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID, lock: bool = False) -> CalendarEvent:
    q = select(CalendarEvent).where(CalendarEvent.id == event_id, CalendarEvent.owner_id == owner_id)
    row = db.execute(q.with_for_update() if lock else q).scalar_one_or_none()
    if row is None:
        raise EventNotFoundError("calendar event is missing or belongs to another owner")
    return row


def create_event(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str,
    starts_at: datetime,
    ends_at: datetime | None = None,
    description: str | None = None,
    location: str | None = None,
) -> CalendarEvent:
    if not title.strip():
        raise InvalidEventTimesError("title must not be empty")
    if ends_at is not None and ends_at <= starts_at:
        raise InvalidEventTimesError("ends_at must be after starts_at")
    row = CalendarEvent(
        owner_id=owner_id,
        title=title,
        description=description,
        location=location,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    db.add(row)
    db.flush()
    return row


def cancel_event(
    db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID, reason: str | None = None
) -> CalendarEvent:
    row = _event(db, owner_id=owner_id, event_id=event_id, lock=True)
    if row.status in _TERMINAL_STATUSES:
        raise InvalidEventTransitionError(f"event is already '{row.status}', cannot cancel")
    row.status = "cancelled"
    row.cancelled_at = datetime.utcnow()  # noqa: DTZ003 -- naive UTC, matching this table's own storage convention
    row.cancellation_reason = reason
    row.updated_at = datetime.utcnow()  # noqa: DTZ003 -- naive UTC, matching this table's own storage convention
    db.flush()
    return row


def complete_event(db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID) -> CalendarEvent:
    row = _event(db, owner_id=owner_id, event_id=event_id, lock=True)
    if row.status in _TERMINAL_STATUSES:
        raise InvalidEventTransitionError(f"event is already '{row.status}', cannot complete")
    row.status = "completed"
    row.updated_at = datetime.utcnow()  # noqa: DTZ003 -- naive UTC, matching this table's own storage convention
    db.flush()
    return row


def get_event(db: Session, *, owner_id: uuid.UUID, event_id: uuid.UUID) -> CalendarEvent:
    return _event(db, owner_id=owner_id, event_id=event_id)


def list_in_range(
    db: Session, *, owner_id: uuid.UUID, starts_after: datetime, starts_before: datetime
) -> list[CalendarEvent]:
    rows = db.execute(
        select(CalendarEvent)
        .where(
            CalendarEvent.owner_id == owner_id,
            CalendarEvent.starts_at >= starts_after,
            CalendarEvent.starts_at < starts_before,
        )
        .order_by(CalendarEvent.starts_at)
    ).scalars().all()
    return list(rows)


def list_upcoming(
    db: Session, *, owner_id: uuid.UUID, within: timedelta | None = None, limit: int = 50
) -> list[CalendarEvent]:
    """The proactive-assistance read path: scheduled (never cancelled/completed) events from
    now onward, soonest first. `within=None` means no upper bound (every future scheduled
    event); pass a `timedelta` to ask "what's coming up in the next N hours/days"."""
    now = datetime.utcnow()  # noqa: DTZ003 -- naive UTC, matching this table's own storage convention
    q = select(CalendarEvent).where(
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.status == "scheduled",
        CalendarEvent.starts_at >= now,
    )
    if within is not None:
        q = q.where(CalendarEvent.starts_at < now + within)
    rows = db.execute(q.order_by(CalendarEvent.starts_at).limit(limit)).scalars().all()
    return list(rows)


@dataclass(frozen=True)
class UpcomingEventSummary:
    """Minimal, read-only projection for cross-module consumption (e.g. a future session-
    context assembler) -- deliberately NOT the ORM row itself, so a consumer never holds a
    live, lockable database object it has no business mutating."""

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
