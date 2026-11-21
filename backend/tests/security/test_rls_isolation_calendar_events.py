"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `calendar_events` (migration 0071 on the 0069-composition
integration branch; 0069 on its own PR branch). Mirrors
tests/security/test_rls_isolation_work_candidates.py's own established pattern exactly --
every new owner-scoped table gets a real behavioral proof, not just a structural one, from
the moment it is added."""

from datetime import datetime, timedelta

from sqlalchemy import text

from app.models.calendar import CalendarEvent


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def test_user_never_reads_another_users_calendar_events(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    starts_at = datetime.utcnow() + timedelta(days=30)  # noqa: DTZ003

    _set_rls_user(db_session, user_a.id)
    db_session.add(CalendarEvent(owner_id=user_a.id, title="A's event", starts_at=starts_at))
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(CalendarEvent(owner_id=user_b.id, title="B's event", starts_at=starts_at))
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(CalendarEvent).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(CalendarEvent).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_calendar_event_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    starts_at = datetime.utcnow() + timedelta(days=30)  # noqa: DTZ003
    _set_rls_user(db_session, user_a.id)
    db_session.add(CalendarEvent(owner_id=user_b.id, title="cross-owner", starts_at=starts_at))
    try:
        db_session.commit()
        raise AssertionError("insert should have been rejected by calendar_events_isolation's WITH CHECK")
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001 -- the exact exception varies by driver/path, matching every sibling RLS-isolation test file exactly
        db_session.rollback()
