"""Regression coverage for backend/scripts/security/ensure_app_role.py.

Not part of the `app` package (it's a standalone pre-boot script, see
docker-entrypoint.sh), so it's loaded directly from its file path rather than imported
normally.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import psycopg2

SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "security" / "ensure_app_role.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ensure_app_role", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fetchone_dispatcher(fake_cursor, *, current_user: str, role_exists: bool):
    """A fully-mocked cursor for these tests only ever needs to answer the two questions the
    pre-S1A version of this script asked (SELECT current_user / role-exists), plus now a
    `to_regclass` table-existence probe and a `pg_proc`/`pg_namespace` function-existence
    probe from `s1a_privilege_policy.apply_privilege_policy(..., require_complete=False)` —
    called UNCONDITIONALLY after the role-provisioning block now (Pass 24: no longer gated
    behind "every S1A object exists first", see ensure_app_role.py's module docstring for
    why), to re-narrow whatever protected tables/functions already exist in the same
    transaction. These tests are about connection-string/pooler edge cases, not S1A privilege
    policy, so every existence probe is answered as "doesn't exist" here (also sets
    `fake_cursor.fetchall.return_value = []` for the function-signature lookup, which uses
    `fetchall()` not `fetchone()`) — with `require_complete=False` that means every managed
    table/function is silently skipped, exactly like a real database that hasn't run
    migration 0019 yet, with no error and nothing narrowed."""
    fake_cursor.fetchall.return_value = []

    def _dispatch(*args, **kwargs):
        executed = fake_cursor.execute.call_args
        sql_text = str(executed.args[0]) if executed and executed.args else ""
        if "current_user" in sql_text:
            return (current_user,)
        if "pg_roles" in sql_text:
            return (1,) if role_exists else None
        if "to_regclass" in sql_text:
            return (False,)
        if "has_schema_privilege" in sql_text:
            return (False,)
        return None

    return _dispatch


def test_default_privileges_use_current_user_not_pooler_login_identity(monkeypatch):
    """Reproduces the Supabase Session Pooler startup crash: DATABASE_URL's username under
    the pooler is a login identity of the form `postgres.<project-ref>` (needed for the
    IPv4 reachability Render requires — see docs/RENDER_DEPLOY.md), not a real Postgres
    role. The pre-fix code passed that URL username straight to `ALTER DEFAULT PRIVILEGES
    FOR ROLE ...`, which fails with `role "postgres.<project-ref>" does not exist` because
    no such role exists in pg_roles — only the mapped real role (`postgres`) does. The fix
    queries `SELECT current_user` for the actual connected role instead."""
    module = _load_module()

    pooler_username = "postgres.ruwihvifpgftcwakdmvo"
    real_role = "postgres"

    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql://{pooler_username}:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setenv("MAINAI_APP_PASSWORD", "app-pw")
    monkeypatch.delenv("RENDER_ENV_FILE", raising=False)

    fake_cursor = MagicMock()
    fake_cursor.__enter__.return_value = fake_cursor
    fake_cursor.__exit__.return_value = False
    # What the pooler-mapped connection actually authenticated as — deliberately different
    # from DATABASE_URL's username, mirroring Supabase's real behavior.
    fake_cursor.fetchone.side_effect = _fetchone_dispatcher(fake_cursor, current_user=real_role, role_exists=True)

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    monkeypatch.setattr(module.psycopg2, "connect", lambda *a, **kw: fake_conn)

    # Spy on sql.Identifier so we can see exactly which role name gets embedded in the
    # ALTER DEFAULT PRIVILEGES statements, without needing a real libpq connection to
    # render the composed SQL to text.
    identifier_args: list[tuple] = []
    real_identifier = module.sql.Identifier

    def _spy_identifier(*args):
        identifier_args.append(args)
        return real_identifier(*args)

    monkeypatch.setattr(module.sql, "Identifier", _spy_identifier)

    module.main()

    # The script must actually ask Postgres for the connected role...
    executed_queries = [call.args[0] for call in fake_cursor.execute.call_args_list]
    assert "SELECT current_user" in executed_queries

    # ...and use that role, never the raw pooler login identity, as a SQL identifier.
    assert (real_role,) in identifier_args
    assert (pooler_username,) not in identifier_args


def test_app_database_url_carries_the_pooler_tenant_suffix(monkeypatch, tmp_path):
    """Reproduces a second, distinct Supabase Session Pooler production crash — this one
    AFTER ensure_app_role.py succeeds: Supavisor rejects any connection whose username lacks
    the `.<tenant-id>` suffix with "no tenant identifier provided (external_id or
    sni_hostname required)", not just the admin connection. The pre-fix code built
    APP_DATABASE_URL's username as the bare role name ("mainai_app"), dropping the tenant
    suffix DATABASE_URL's own username carries (`postgres.<project-ref>`) — so every runtime
    request-serving connection the app made after boot was rejected by the pooler, even
    though ensure_app_role.py itself (using the admin DATABASE_URL, which does have the
    suffix) had just succeeded moments earlier. The fix copies the tenant suffix onto the
    app role's username too."""
    module = _load_module()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.ruwihvifpgftcwakdmvo:adminpw@aws-1-us-west-2.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setenv("MAINAI_APP_PASSWORD", "app-pw")
    env_file = tmp_path / "render_env.sh"
    monkeypatch.setenv("RENDER_ENV_FILE", str(env_file))

    fake_cursor = MagicMock()
    fake_cursor.__enter__.return_value = fake_cursor
    fake_cursor.__exit__.return_value = False
    fake_cursor.fetchone.side_effect = _fetchone_dispatcher(fake_cursor, current_user="postgres", role_exists=True)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    monkeypatch.setattr(module.psycopg2, "connect", lambda *a, **kw: fake_conn)

    module.main()

    written = env_file.read_text()
    assert "APP_DATABASE_URL=" in written
    app_url = written.split('APP_DATABASE_URL="', 1)[1].split('"', 1)[0]
    app_username = urlparse(app_url).username
    assert app_username == "mainai_app.ruwihvifpgftcwakdmvo"


def test_app_database_url_stays_unsuffixed_for_a_plain_non_pooled_admin_username(monkeypatch, tmp_path):
    """Local Docker Compose / any direct (non-pooled) Postgres has a plain admin username
    (e.g. `lifeos`) with no tenant suffix to copy — must not gain a spurious ".something"
    appended, which would just be a made-up role name that doesn't exist."""
    module = _load_module()

    monkeypatch.setenv("DATABASE_URL", "postgresql://lifeos:adminpw@localhost:5432/lifeos")
    monkeypatch.setenv("MAINAI_APP_PASSWORD", "app-pw")
    env_file = tmp_path / "render_env.sh"
    monkeypatch.setenv("RENDER_ENV_FILE", str(env_file))

    fake_cursor = MagicMock()
    fake_cursor.__enter__.return_value = fake_cursor
    fake_cursor.__exit__.return_value = False
    fake_cursor.fetchone.side_effect = _fetchone_dispatcher(fake_cursor, current_user="lifeos", role_exists=True)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    monkeypatch.setattr(module.psycopg2, "connect", lambda *a, **kw: fake_conn)

    module.main()

    written = env_file.read_text()
    app_url = written.split('APP_DATABASE_URL="', 1)[1].split('"', 1)[0]
    assert urlparse(app_url).username == "mainai_app"


def test_password_is_not_rotated_on_a_normal_restart_when_the_role_already_exists(monkeypatch):
    """The actual verified production incident: the pre-fix script ran `ALTER ROLE ...
    PASSWORD` unconditionally on every single boot, even when mainai_app already existed
    with the correct password. Under Supabase's Session Pooler, that put every ordinary
    restart at risk of a transient "password authentication failed" on the very next
    connection while Supavisor's own auth cache caught up (see the module docstring) — a
    real deploy hit exactly this and crashed. A normal restart against an
    already-provisioned role must never touch the password, and must therefore never need
    the self-test-connect retry either (nothing changed to test)."""
    module = _load_module()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.ruwihvifpgftcwakdmvo:adminpw@aws-1-us-west-2.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setenv("MAINAI_APP_PASSWORD", "app-pw")
    monkeypatch.delenv("MAINAI_APP_ROTATE_PASSWORD", raising=False)
    monkeypatch.delenv("RENDER_ENV_FILE", raising=False)

    fake_cursor = MagicMock()
    fake_cursor.__enter__.return_value = fake_cursor
    fake_cursor.__exit__.return_value = False
    fake_cursor.fetchone.side_effect = _fetchone_dispatcher(fake_cursor, current_user="postgres", role_exists=True)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    connect_calls: list[tuple] = []

    def _fake_connect(*a, **kw):
        connect_calls.append((a, kw))
        return fake_conn

    monkeypatch.setattr(module.psycopg2, "connect", _fake_connect)

    executed_sql_templates: list[str] = []
    real_sql = module.sql.SQL

    def _spy_sql(text_arg):
        executed_sql_templates.append(text_arg)
        return real_sql(text_arg)

    monkeypatch.setattr(module.sql, "SQL", _spy_sql)

    module.main()

    assert not any("CREATE ROLE" in t or "ALTER ROLE" in t for t in executed_sql_templates)
    # Only the one admin connection — no self-test-connect call, since the password was
    # never touched.
    assert len(connect_calls) == 1


def test_explicit_rotate_password_env_var_rotates_and_self_tests_the_new_credential(monkeypatch):
    """MAINAI_APP_ROTATE_PASSWORD=true is the explicit, one-off opt-in an operator uses when
    they actually want to rotate the password on a specific deploy — this is the ONE case
    where the password does change on a boot against an already-existing role, and where the
    self-test-connect retry (the actual fix for the production incident) must run."""
    module = _load_module()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.ruwihvifpgftcwakdmvo:adminpw@aws-1-us-west-2.pooler.supabase.com:5432/postgres",
    )
    monkeypatch.setenv("MAINAI_APP_PASSWORD", "app-pw")
    monkeypatch.setenv("MAINAI_APP_ROTATE_PASSWORD", "true")
    monkeypatch.delenv("RENDER_ENV_FILE", raising=False)

    fake_cursor = MagicMock()
    fake_cursor.__enter__.return_value = fake_cursor
    fake_cursor.__exit__.return_value = False
    fake_cursor.fetchone.side_effect = _fetchone_dispatcher(fake_cursor, current_user="postgres", role_exists=True)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor

    connect_calls: list[tuple] = []

    def _fake_connect(*a, **kw):
        connect_calls.append((a, kw))
        return fake_conn

    monkeypatch.setattr(module.psycopg2, "connect", _fake_connect)

    executed_sql_templates: list[str] = []
    real_sql = module.sql.SQL

    def _spy_sql(text_arg):
        executed_sql_templates.append(text_arg)
        return real_sql(text_arg)

    monkeypatch.setattr(module.sql, "SQL", _spy_sql)

    module.main()

    assert any("ALTER ROLE" in t for t in executed_sql_templates)
    # The admin connection, plus at least one self-test-connect attempt against the new
    # credential.
    assert len(connect_calls) >= 2


def test_self_test_connection_retries_transient_failures_then_succeeds(monkeypatch):
    """Unit-level proof of the retry/backoff mechanism itself — the same mechanism proven
    end-to-end against the real Dockerfile.combined image in combined-container-verify's
    delayed-app-role container (see .github/workflows/ci.yml)."""
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    call_count = {"n": 0}

    def _flaky_connect(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise module.psycopg2.OperationalError("simulated transient failure")
        return MagicMock()

    monkeypatch.setattr(module.psycopg2, "connect", _flaky_connect)
    module._self_test_connection("postgresql://irrelevant", attempts=5, base_delay_seconds=0.01)
    assert call_count["n"] == 3


def test_self_test_connection_gives_up_and_raises_after_exhausting_every_attempt(monkeypatch):
    """A connection that NEVER succeeds must still fail loudly (not hang forever, not be
    silently swallowed) — this is what distinguishes a real wrong credential from transient
    pooler propagation lag."""
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)

    def _always_fails(*a, **kw):
        raise module.psycopg2.OperationalError("permanently wrong password")

    monkeypatch.setattr(module.psycopg2, "connect", _always_fails)

    import pytest

    with pytest.raises(RuntimeError, match="efter 3 försök"):
        module._self_test_connection("postgresql://irrelevant", attempts=3, base_delay_seconds=0.01)


def test_full_script_run_against_real_local_postgres_is_idempotent(monkeypatch):
    """Runs the real, unmocked script against the actual local Postgres test database this
    suite already uses (see conftest.py's `_test_database` fixture) — the ordinary
    local/Docker-Compose-style case where DATABASE_URL's username already IS a real role
    (e.g. `lifeos`), not a pooler login identity. Confirms the current_user fix doesn't
    regress that setup: `current_user` there must simply equal the connection's own
    username, and running the script twice (as every container restart does) must stay
    safely idempotent."""
    from app.config import get_settings

    settings = get_settings()
    app_password = urlparse(settings.app_database_url).password or "mainai_app_pw"

    module = _load_module()
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    # Same password conftest.py's _test_database fixture already provisioned mainai_app
    # with — running this script must not change it out from under the rest of the suite.
    monkeypatch.setenv("MAINAI_APP_PASSWORD", app_password)
    monkeypatch.delenv("RENDER_ENV_FILE", raising=False)

    module.main()
    module.main()  # a second, idempotent run — what every container restart actually does

    # mainai_app must still be reachable with the same password afterwards.
    conn = psycopg2.connect(settings.app_database_url)
    conn.close()


def test_s1a_renarrowing_failure_rolls_back_the_whole_transaction_not_just_itself(monkeypatch):
    """The concrete requirement this proves: a failure partway through S1A re-narrowing must
    never leave the broader `GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public` (which runs
    earlier in the SAME transaction) as the committed end state. Forces
    apply_privilege_policy() to report an error and confirms mainai_app's privileges on
    memory_source_units are STILL narrowed afterward — proving the whole transaction,
    GRANT ALL included, rolled back together, not just the failed narrowing step."""
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy import text as sa_text

    from app.config import get_settings

    settings = get_settings()
    app_password = urlparse(settings.app_database_url).password or "mainai_app_pw"

    # Establish a known-good, narrowed baseline first (real apply_runtime_privileges.py run).
    apply_runtime_privileges_path = SCRIPT_PATH.parent / "apply_runtime_privileges.py"
    apply_spec = importlib.util.spec_from_file_location("apply_runtime_privileges", apply_runtime_privileges_path)
    apply_module = importlib.util.module_from_spec(apply_spec)
    apply_spec.loader.exec_module(apply_module)
    apply_module.apply_and_verify(settings.database_url)

    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            has_update_before = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            ).scalar()
        assert has_update_before is False, "test setup: baseline should already be narrowed"

        module = _load_module()
        monkeypatch.setenv("DATABASE_URL", settings.database_url)
        monkeypatch.setenv("MAINAI_APP_PASSWORD", app_password)
        monkeypatch.delenv("RENDER_ENV_FILE", raising=False)
        monkeypatch.setattr(
            module,
            "apply_privilege_policy",
            lambda cur, *, expected_owner, require_complete=True: ["forced test failure"],
        )

        with pytest.raises(RuntimeError, match="forced test failure"):
            module.main()

        with engine.connect() as conn:
            has_update_after = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            ).scalar()
        assert has_update_after is False, (
            "ensure_app_role's broad GRANT ALL survived a re-narrowing failure — the "
            "transaction did not roll back atomically"
        )
    finally:
        engine.dispose()
        # Restore a clean, fully-narrowed state for any tests that run after this one.
        apply_module.apply_and_verify(settings.database_url)
