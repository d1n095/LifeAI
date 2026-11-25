"""Real-PostgreSQL regression for the billing period_start DATE contract (2026-11-18).

The entire billing suite runs on in-memory SQLite (test_billing_engine.py's engine_db
fixture) where period_start is TEXT, so a bare "%Y-%m" month key silently string-compared
and the bug was invisible there. On the real database — the only thing production runs on —
usage_rollups.period_start is a DATE column, and every record_usage call failed with
``InvalidDatetimeFormat: invalid input syntax for type date: "2026-11"``, aborting the
request's transaction mid-chat (2026-11-18 real-PG sweep: 30 direct + ~52 cascade failures
across tests/backend/rag and tests/backend/chat; reproduced identically on the frozen
candidate a1a0e073, so it predates this branch). These tests run against the session's
REAL Postgres so the date contract can never silently diverge between the two dialects
again.
"""

from datetime import datetime, timezone

from sqlalchemy import text

from app.billing import record_usage


def _rollup_period_start(db, subject_type, subject_id, period_type):
    return db.execute(
        text(
            "SELECT period_start::text FROM usage_rollups "
            "WHERE subject_type=:st AND subject_id=:sid AND period_type=:pt"
        ),
        {"st": subject_type, "sid": str(subject_id), "pt": period_type},
    ).scalar()


def test_record_usage_month_rollup_is_anchored_at_the_first_of_month(db_session, make_verified_user):
    """The exact statement that failed on real PostgreSQL before the fix — a plain chat
    usage event — must now land BOTH rollups with DATE-clean period keys."""
    user, _ = make_verified_user()
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    record_usage(
        db_session,
        user_id=user.id,
        tenant_id=None,
        role="chat",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=10,
        completion_tokens=5,
    )
    db_session.commit()

    now = datetime.now(timezone.utc)
    assert _rollup_period_start(db_session, "user", user.id, "day") == now.strftime("%Y-%m-%d")
    assert _rollup_period_start(db_session, "user", user.id, "month") == now.strftime("%Y-%m-01")


def test_record_usage_month_rollup_counts_tokens_not_just_dates(db_session, make_verified_user):
    """Guards against the fix having anchored the date but lost the increment (e.g. a
    write that silently skipped the rollup): the month rollup must carry the same totals
    as the day rollup for a single event."""
    user, _ = make_verified_user()
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    record_usage(
        db_session,
        user_id=user.id,
        tenant_id=None,
        role="chat",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
    )
    db_session.commit()

    row = db_session.execute(
        text(
            "SELECT period_type, tokens_in, tokens_out FROM usage_rollups "
            "WHERE subject_type='user' AND subject_id=:sid"
        ),
        {"sid": str(user.id)},
    ).all()
    by_period = {period: (tin, tout) for period, tin, tout in row}
    assert by_period == {"day": (100, 50), "month": (100, 50)}
