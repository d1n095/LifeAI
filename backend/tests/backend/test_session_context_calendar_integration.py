"""Connects two already-implemented, previously-disconnected modules: `app.calendar` (PR #411)
and `app.session_context` (PR #381, integrated in PR #414). `app.calendar.service`'s own
docstring named "a future session-context assembler" as `upcoming_summaries()`'s intended
consumer before any such wiring existed -- this is that wiring, plus the executable proof it
actually works end to end against real Postgres.

Founder directive 2026-11-15 ("Continue Active Development"), Priority 3: "Prioritize missing
connections between already-implemented modules rather than creating new standalone features."
"""

from datetime import datetime, timedelta

from sqlalchemy import text as sa_text

from app import session_context as sc
from app.calendar import create_event
from app.request_context import current_user_id as current_user_id_var


def _rls(db, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def test_upcoming_calendar_event_is_surfaced_in_session_context(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)

    starts_at = datetime.utcnow() + timedelta(days=2)  # noqa: DTZ003 -- naive UTC, matching this table's own convention
    event = create_event(db_session, owner_id=user.id, title="Founder sync", starts_at=starts_at, location="Zoom")
    db_session.commit()

    bundle = sc.assemble_session_context(db_session, owner_id=user.id, session_id="session-1")

    section = bundle.section("upcoming_calendar_events")
    assert section is not None
    assert section.source_module == "calendar"
    assert section.error is None
    assert len(section.items) == 1
    item = section.items[0]
    assert item["source_ref"] == str(event.id)
    assert item["title"] == "Founder sync"
    assert item["location"] == "Zoom"


def test_calendar_events_outside_the_seven_day_window_are_not_surfaced(db_session, make_verified_user):
    user, _ = make_verified_user()
    _rls(db_session, user.id)

    far_future = datetime.utcnow() + timedelta(days=30)  # noqa: DTZ003
    create_event(db_session, owner_id=user.id, title="Next month", starts_at=far_future)
    db_session.commit()

    bundle = sc.assemble_session_context(db_session, owner_id=user.id, session_id="session-1")

    section = bundle.section("upcoming_calendar_events")
    assert section is not None
    assert section.items == ()


def test_cancelled_calendar_events_are_not_surfaced_as_upcoming(db_session, make_verified_user):
    from app.calendar import cancel_event

    user, _ = make_verified_user()
    _rls(db_session, user.id)

    soon = datetime.utcnow() + timedelta(hours=6)  # noqa: DTZ003
    event = create_event(db_session, owner_id=user.id, title="About to be cancelled", starts_at=soon)
    db_session.commit()
    cancel_event(db_session, owner_id=user.id, event_id=event.id)
    db_session.commit()

    bundle = sc.assemble_session_context(db_session, owner_id=user.id, session_id="session-1")

    section = bundle.section("upcoming_calendar_events")
    assert section is not None
    assert section.items == ()


def test_another_owners_calendar_events_are_never_surfaced(db_session, make_verified_user):
    """Tenant isolation, exercised through the full chain (RLS + app.calendar's own owner_id
    filtering), not just unit-tested in isolation."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    _rls(db_session, owner_a.id)
    create_event(db_session, owner_id=owner_a.id, title="A's event", starts_at=datetime.utcnow() + timedelta(days=1))  # noqa: DTZ003
    db_session.commit()

    _rls(db_session, owner_b.id)
    bundle = sc.assemble_session_context(db_session, owner_id=owner_b.id, session_id="session-1")

    section = bundle.section("upcoming_calendar_events")
    assert section is not None
    assert section.items == ()


def test_a_calendar_source_failure_does_not_block_the_whole_session_context(db_session, make_verified_user, monkeypatch):
    """Best-effort section, same discipline every other non-critical section in this assembler
    already uses (_safe_items()): a broken calendar read is recorded on its own section, never
    escalated into `blocked`, never crashes the rest of the bundle."""
    user, _ = make_verified_user()
    _rls(db_session, user.id)

    monkeypatch.setattr(
        "app.calendar.upcoming_summaries", lambda db, *, owner_id, within, limit=20: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    bundle = sc.assemble_session_context(db_session, owner_id=user.id, session_id="session-1")

    section = bundle.section("upcoming_calendar_events")
    assert section is not None
    assert section.error is not None
    assert "boom" in section.error
    assert bundle.blocked is False, "a calendar read failure must never escalate to blocked"
