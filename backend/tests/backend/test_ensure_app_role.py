"""Regression coverage for backend/scripts/ensure_app_role.py.

Not part of the `app` package (it's a standalone pre-boot script, see
docker-entrypoint.sh), so it's loaded directly from its file path rather than imported
normally.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

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
