"""Regression coverage for app/db.py's call_with_db_retry — the general-purpose retry/backoff
wrapper used around app/main.py's startup-time DB touches.

Verified production incident, 2026-07-20: a Supabase Session Pooler auth-cache propagation
lag right after backend/scripts/ensure_app_role.py provisioned or changed the mainai_app
role's password made the FIRST connection app/main.py's on_startup() ever made fail with
"password authentication failed" — with no retry anywhere in that path, that took the whole
uvicorn process down ("Application startup failed. Exiting.") even though the exact same
credential worked moments later. call_with_db_retry exists so a transient connection failure
at startup doesn't kill an otherwise-healthy container.
"""

import pytest
from sqlalchemy.exc import OperationalError

from app.db import call_with_db_retry


def test_succeeds_immediately_when_the_first_attempt_works():
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        return "ok"

    assert call_with_db_retry(_fn, attempts=3, base_delay_seconds=0.01) == "ok"
    assert calls["n"] == 1


def test_retries_a_transient_operational_error_then_succeeds(monkeypatch):
    """Reproduces the actual incident: the first attempt(s) hit the pooler's stale auth
    cache and fail; a later attempt, with the identical (correct) credential, succeeds."""
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("connection failed", {}, Exception("password authentication failed"))
        return "ok"

    result = call_with_db_retry(_fn, attempts=5, base_delay_seconds=0.01)
    assert result == "ok"
    assert calls["n"] == 3


def test_reraises_after_exhausting_every_attempt():
    """A connection that never succeeds must still fail loudly — this distinguishes a
    genuinely broken credential/unreachable database from transient pooler propagation lag,
    and must still stop the app from serving traffic under a false sense of health."""
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise OperationalError("connection failed", {}, Exception("password authentication failed"))

    with pytest.raises(OperationalError):
        call_with_db_retry(_fn, attempts=3, base_delay_seconds=0.01)
    assert calls["n"] == 3


def test_does_not_retry_a_non_operational_error():
    """Only connection-level failures are worth retrying — a genuine application bug (e.g. a
    programming error inside fn()) must surface immediately, not be masked behind several
    seconds of pointless retries."""
    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        raise ValueError("not a connection problem")

    with pytest.raises(ValueError):
        call_with_db_retry(_fn, attempts=3, base_delay_seconds=0.01)
    assert calls["n"] == 1
