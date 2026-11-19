"""Founder calendar foundation (migration 0069) -- service-layer tests, RLS bypassed via
`superuser_db` (same pattern as other non-RLS-focused service tests in this codebase, e.g.
tests/backend/mainai/test_system_activation_authorization.py's own `current_system_activation`
tests) since these exercise `app.calendar.service`'s own owner_id filtering, not Postgres RLS
itself -- that is proven separately in tests/security/test_rls_isolation_calendar_events.py."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.calendar import (
    EventNotFoundError,
    InvalidEventTimesError,
    InvalidEventTransitionError,
    cancel_event,
    complete_event,
    create_event,
    get_event,
    list_in_range,
    list_upcoming,
    upcoming_summaries,
)
from app.models.user import User


def _owner(db) -> User:
    user = User(email=f"cal-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_create_event_requires_a_non_empty_title(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    with pytest.raises(InvalidEventTimesError):
        create_event(superuser_db, owner_id=owner.id, title="   ", starts_at=datetime.utcnow())  # noqa: DTZ003


def test_create_event_rejects_ends_at_before_starts_at(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    starts_at = datetime.utcnow()  # noqa: DTZ003
    with pytest.raises(InvalidEventTimesError):
        create_event(superuser_db, owner_id=owner.id, title="Meeting", starts_at=starts_at, ends_at=starts_at - timedelta(hours=1))


def test_create_and_get_event_round_trips(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    starts_at = datetime.utcnow() + timedelta(days=1)  # noqa: DTZ003
    created = create_event(
        superuser_db, owner_id=owner.id, title="Founder sync", starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1), description="weekly", location="HQ",
    )
    superuser_db.commit()

    fetched = get_event(superuser_db, owner_id=owner.id, event_id=created.id)
    assert fetched.id == created.id
    assert fetched.title == "Founder sync"
    assert fetched.status == "scheduled"
    assert fetched.source == "manual"


def test_get_event_raises_for_another_owner(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()
    event = create_event(superuser_db, owner_id=owner_a.id, title="Private", starts_at=datetime.utcnow())  # noqa: DTZ003
    superuser_db.commit()

    with pytest.raises(EventNotFoundError):
        get_event(superuser_db, owner_id=owner_b.id, event_id=event.id)


def test_cancel_event_sets_status_and_reason(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    event = create_event(superuser_db, owner_id=owner.id, title="Call", starts_at=datetime.utcnow())  # noqa: DTZ003
    superuser_db.commit()

    cancelled = cancel_event(superuser_db, owner_id=owner.id, event_id=event.id, reason="founder unavailable")
    superuser_db.commit()
    assert cancelled.status == "cancelled"
    assert cancelled.cancellation_reason == "founder unavailable"
    assert cancelled.cancelled_at is not None


def test_cancel_event_twice_raises_invalid_transition(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    event = create_event(superuser_db, owner_id=owner.id, title="Call", starts_at=datetime.utcnow())  # noqa: DTZ003
    superuser_db.commit()
    cancel_event(superuser_db, owner_id=owner.id, event_id=event.id)
    superuser_db.commit()

    with pytest.raises(InvalidEventTransitionError):
        cancel_event(superuser_db, owner_id=owner.id, event_id=event.id)


def test_complete_event_then_cancel_raises_invalid_transition(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    event = create_event(superuser_db, owner_id=owner.id, title="Review", starts_at=datetime.utcnow())  # noqa: DTZ003
    superuser_db.commit()
    complete_event(superuser_db, owner_id=owner.id, event_id=event.id)
    superuser_db.commit()

    with pytest.raises(InvalidEventTransitionError):
        cancel_event(superuser_db, owner_id=owner.id, event_id=event.id)


def test_list_upcoming_excludes_past_and_cancelled_events(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    now = datetime.utcnow()  # noqa: DTZ003

    past = create_event(superuser_db, owner_id=owner.id, title="Past", starts_at=now - timedelta(days=1))
    future_cancelled = create_event(superuser_db, owner_id=owner.id, title="Future cancelled", starts_at=now + timedelta(days=1))
    future_scheduled = create_event(superuser_db, owner_id=owner.id, title="Future scheduled", starts_at=now + timedelta(days=2))
    superuser_db.commit()
    cancel_event(superuser_db, owner_id=owner.id, event_id=future_cancelled.id)
    superuser_db.commit()

    upcoming = list_upcoming(superuser_db, owner_id=owner.id)
    upcoming_ids = {row.id for row in upcoming}
    assert past.id not in upcoming_ids
    assert future_cancelled.id not in upcoming_ids
    assert future_scheduled.id in upcoming_ids


def test_list_upcoming_respects_within_window(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    now = datetime.utcnow()  # noqa: DTZ003

    soon = create_event(superuser_db, owner_id=owner.id, title="Soon", starts_at=now + timedelta(hours=2))
    later = create_event(superuser_db, owner_id=owner.id, title="Later", starts_at=now + timedelta(days=10))
    superuser_db.commit()

    within_a_day = list_upcoming(superuser_db, owner_id=owner.id, within=timedelta(days=1))
    within_ids = {row.id for row in within_a_day}
    assert soon.id in within_ids
    assert later.id not in within_ids


def test_list_in_range_is_half_open_and_owner_scoped(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()
    start = datetime.utcnow() + timedelta(days=30)  # noqa: DTZ003
    inside = create_event(superuser_db, owner_id=owner_a.id, title="Inside", starts_at=start + timedelta(hours=1))
    at_boundary = create_event(superuser_db, owner_id=owner_a.id, title="At end boundary", starts_at=start + timedelta(days=1))
    other_owner_event = create_event(superuser_db, owner_id=owner_b.id, title="Other owner", starts_at=start + timedelta(hours=2))
    superuser_db.commit()

    rows = list_in_range(superuser_db, owner_id=owner_a.id, starts_after=start, starts_before=start + timedelta(days=1))
    ids = {row.id for row in rows}
    assert inside.id in ids
    assert at_boundary.id not in ids, "starts_before is an exclusive upper bound"
    assert other_owner_event.id not in ids


def test_upcoming_summaries_are_a_plain_read_only_projection(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    now = datetime.utcnow()  # noqa: DTZ003
    create_event(superuser_db, owner_id=owner.id, title="Demo", starts_at=now + timedelta(hours=1), location="Zoom")
    superuser_db.commit()

    summaries = upcoming_summaries(superuser_db, owner_id=owner.id, within=timedelta(days=1))
    assert len(summaries) == 1
    assert summaries[0].title == "Demo"
    assert summaries[0].location == "Zoom"
    assert not hasattr(summaries[0], "owner_id")
