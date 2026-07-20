"""Regression coverage for backend/scripts/ensure_app_role.py.

Not part of the `app` package (it's a standalone pre-boot script, see
docker-entrypoint.sh), so it's loaded directly from its file path rather than imported
normally.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlparse

import psycopg2

SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "ensure_app_role.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ensure_app_role", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    fake_cursor.fetchone.return_value = (real_role,)

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
    fake_cursor.fetchone.return_value = ("postgres",)
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
    fake_cursor.fetchone.return_value = ("lifeos",)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    monkeypatch.setattr(module.psycopg2, "connect", lambda *a, **kw: fake_conn)

    module.main()

    written = env_file.read_text()
    app_url = written.split('APP_DATABASE_URL="', 1)[1].split('"', 1)[0]
    assert urlparse(app_url).username == "mainai_app"


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
