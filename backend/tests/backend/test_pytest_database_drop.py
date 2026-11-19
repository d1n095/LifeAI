"""Session-setup DROP DATABASE must terminate leftover backends first (ObjectInUse)."""

import pytest
import psycopg2
from urllib.parse import urlparse

from app.config import get_settings
from tests.conftest import _assert_disposable_database, _force_drop_database, _server_dsn


def test_force_drop_database_rejects_non_identifier_names():
    class _Cur:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("must not run SQL for a non-identifier name")

    with pytest.raises(ValueError, match="non-identifier"):
        _force_drop_database(_Cur(), "lifeos_test; DROP DATABASE postgres")


def test_the_suite_refuses_to_drop_an_installed_application_database():
    """A real founder database was dropped this way, so the refusal is a test, not a comment.

    The suite is autouse and session-scoped: it DROPs the database `DATABASE_URL` names at
    teardown. Exporting an installed database into a pytest run therefore deleted it. The guard
    rejects anything that is not a `lifeos_test*` name before the first DROP.
    """
    for name in ("lifeos_founder_flow", "lifeos_accept", "postgres", "lifeaI_test_x"):
        with pytest.raises(pytest.UsageError, match="refusing to drop database"):
            _assert_disposable_database(name)

    for name in ("lifeos_test", "lifeos_test_4242", "lifeos_test_a1b2c3"):
        _assert_disposable_database(name)  # does not raise


def test_force_drop_database_succeeds_while_another_session_is_connected():
    settings = get_settings()
    scratch = "lifeos_drop_probe_" + urlparse(settings.database_url).path.lstrip("/")[-8:]
    admin = psycopg2.connect(_server_dsn(settings.database_url))
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            _force_drop_database(cur, scratch)
            cur.execute(f'CREATE DATABASE "{scratch}"')
        holder = psycopg2.connect(_server_dsn(settings.database_url, dbname=scratch))
        try:
            with admin.cursor() as cur:
                _force_drop_database(cur, scratch)
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (scratch,))
                assert cur.fetchone() is None
        finally:
            holder.close()
    finally:
        with admin.cursor() as cur:
            _force_drop_database(cur, scratch)
        admin.close()
