"""SFA-1 — the runtime role must hold NO privilege of any kind on migration state
(`alembic_version`).

Finding (independent review of the integrated Founder Alpha candidate): `mainai_app` held
SELECT/INSERT/UPDATE/DELETE on `alembic_version`, as collateral of the historical
`GRANT ALL PRIVILEGES ON ALL TABLES`. The schema-wide floor
(`_NEVER_GRANTED_TABLE_PRIVS`) deliberately leaves the four DML privileges alone — they are
genuinely needed on user-data tables — so nothing narrowed this one.

Why it is a real risk rather than tidiness: `alembic_version` holds one row naming the revision
the schema is actually at, and deleting that row does not undo anything, it *lies*. The next
`alembic upgrade head` would no longer see the head as applied and would attempt its DDL a
second time against a database that already has it — from an ordinary request path, and with no
RLS in the way (RLS is row-scoped; this table has no policy). A forged UPDATE is the mirror
image: claiming "already at head" makes a restart skip migrations it believes are done. No code
under `app/` or `scripts/` reads `alembic_version` at all, and Alembic itself only ever runs on
the admin/migration connection (`backend/alembic/env.py` → `settings.database_url`; see
`backend/docker-entrypoint.sh`'s boot order), so the runtime role has no legitimate use for it.

Assertions here are made against Postgres's own live catalog/ACL answers
(`has_table_privilege`) AND against the server actually refusing the statement as the real
restricted role — the same two-claims standard `test_runtime_table_privileges.py` states: a
catalog reading and the server's actual behaviour are different claims, and it is the second
one the security property depends on. The last test runs the real `alembic upgrade head`
command after the revocation to prove the migration path itself is untouched.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import migration_engine

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_PATH = BACKEND_ROOT / "scripts" / "security" / "s1a_privilege_policy.py"

MIGRATION_STATE_TABLE = "alembic_version"


def _load_policy():
    """The policy lives outside the `app` package on purpose (it must run standalone, pre-boot,
    before `app` is importable — see backend/docker-entrypoint.sh), so it is loaded by path,
    the same way tests/backend/test_runtime_table_privileges.py does."""
    spec = importlib.util.spec_from_file_location("s1a_privilege_policy", POLICY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def policy_applied():
    """Runs the REAL boot-time policy — the same `apply_privilege_policy()` call
    `ensure_app_role.py` and `apply_runtime_privileges.py` both make, in its own transaction
    with the same advisory lock — because the session fixture's privilege baseline
    (`GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES`) is the *legacy wide* state for this
    table, exactly like a production database that has booted once.

    `require_complete=True` is the apply_runtime_privileges.py path (after `alembic upgrade
    head`): every managed object, `alembic_version` included, must already exist here."""
    policy = _load_policy()
    admin = psycopg2.connect(get_settings().database_url)
    admin.autocommit = False
    try:
        with admin.cursor() as cur:
            policy.acquire_privilege_boot_lock(cur)
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = policy.apply_privilege_policy(
                cur, expected_owner=expected_owner, require_complete=True
            )
            assert errors == [], f"policy reported errors: {errors}"
        admin.commit()
    finally:
        admin.rollback()
        admin.close()
    return policy


# --------------------------------------------------------------------------------------
# 1. The policy really revokes all seven privileges
# --------------------------------------------------------------------------------------


def test_policy_revokes_every_privilege_on_alembic_version_from_the_runtime_role(policy_applied):
    policy = policy_applied
    held = {}
    with migration_engine.connect() as conn:
        for priv in policy._ALL_TABLE_PRIVS:
            held[priv] = bool(
                conn.execute(
                    text("SELECT has_table_privilege('mainai_app', :t, :p)"),
                    {"t": f"public.{MIGRATION_STATE_TABLE}", "p": priv},
                ).scalar()
            )
    assert held == {priv: False for priv in policy._ALL_TABLE_PRIVS}, (
        "mainai_app still holds privileges on alembic_version: "
        f"{[p for p, has in held.items() if has]}"
    )


def test_policy_is_idempotent_on_a_second_boot(policy_applied):
    """Every boot re-runs the policy against already-narrowed state (that is the whole point of
    it being a boot step, not a migrated privilege). A second run must be a clean no-op, not an
    error — otherwise an ordinary restart of an already-correct database would fail closed."""
    policy = policy_applied
    admin = psycopg2.connect(get_settings().database_url)
    admin.autocommit = False
    try:
        with admin.cursor() as cur:
            policy.acquire_privilege_boot_lock(cur)
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = policy.apply_privilege_policy(
                cur, expected_owner=expected_owner, require_complete=True
            )
            assert errors == [], f"second policy run reported errors: {errors}"
        admin.commit()
    finally:
        admin.rollback()
        admin.close()


# --------------------------------------------------------------------------------------
# 2. Mutation tests — the server genuinely denies it, as the real runtime role
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM alembic_version",
        "UPDATE alembic_version SET version_num = 'forged'",
        "INSERT INTO alembic_version (version_num) VALUES ('forged')",
        "SELECT version_num FROM alembic_version",
    ],
    ids=["delete", "update", "insert", "select"],
)
def test_runtime_role_is_refused_by_postgres_itself(policy_applied, statement):
    """Connects as `mainai_app` — the real credential the application serves traffic with
    (`APP_DATABASE_URL`), not a mock and not the admin role — and issues the statement the
    finding was about. Postgres must reject it, not merely report the privilege as absent."""
    conn = psycopg2.connect(get_settings().app_database_url)
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(statement)
    finally:
        conn.rollback()
        conn.close()


def test_the_recorded_revision_is_still_intact_after_those_attempts(policy_applied):
    """The row must still name the real single head — the point of the finding is that a
    successful DELETE/UPDATE would leave the schema mislabelled, so this asserts the label."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected exactly one Alembic head, found {heads}"

    with migration_engine.connect() as conn:
        recorded = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    assert recorded == [heads[0]], (
        f"alembic_version records {recorded}, expected exactly the single head {heads[0]}"
    )


# --------------------------------------------------------------------------------------
# 3. The fix repairs an already-wide database and does not break migrations
# --------------------------------------------------------------------------------------


def test_legacy_wide_grant_is_narrowed_for_migration_state(policy_applied):
    """A GRANT never removes privileges, so every database that has booted once already has the
    wide ACL durably committed. Reproduce that legacy state explicitly, then assert the
    boot-time policy takes it back down — otherwise this fix would only ever help new
    databases."""
    policy = policy_applied
    admin = psycopg2.connect(get_settings().database_url)
    admin.autocommit = False
    try:
        with admin.cursor() as cur:
            cur.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainai_app")
            admin.commit()

            cur.execute("SELECT has_table_privilege('mainai_app', 'public.alembic_version', 'DELETE')")
            assert cur.fetchone()[0] is True, "precondition failed: legacy wide grant did not apply"

            policy.acquire_privilege_boot_lock(cur)
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = policy.apply_privilege_policy(
                cur, expected_owner=expected_owner, require_complete=False
            )
            assert errors == [], f"policy reported errors: {errors}"
            admin.commit()

            for priv in policy._ALL_TABLE_PRIVS:
                cur.execute(
                    "SELECT has_table_privilege('mainai_app', 'public.alembic_version', %s)", (priv,)
                )
                assert cur.fetchone()[0] is False, f"alembic_version.{priv} survived the policy run"
    finally:
        admin.rollback()
        admin.close()


def test_the_migration_role_keeps_full_write_access_to_its_own_bookkeeping(policy_applied):
    """The half of "least privilege" that must not be over-applied: Alembic's own role (the
    admin/migration connection) still owns and can rewrite the row. Asserted by performing a
    real no-op UPDATE and rolling it back, not by reading a catalog flag alone."""
    # `Connection.execute()` already opened the transaction implicitly (SQLAlchemy 2.0), so the
    # write commitment is undone with `rollback()` rather than a second `begin()`.
    with migration_engine.connect() as conn:
        can_write = conn.execute(
            text("SELECT has_table_privilege(current_user, 'public.alembic_version', 'UPDATE')")
        ).scalar()
        assert can_write is True, "the migration role lost UPDATE on alembic_version"
        conn.execute(text("UPDATE alembic_version SET version_num = version_num"))
        conn.rollback()


def test_alembic_upgrade_head_still_runs_after_the_revocation(policy_applied):
    """The direct answer to "provided it does not break legitimate migrations": run the exact
    command `backend/docker-entrypoint.sh` runs, after the runtime role has been stripped. It
    must exit 0 — and it is the admin connection that does the work, which is precisely why the
    runtime role's privileges on this table are irrelevant to it."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    assert result.returncode == 0, (
        "alembic upgrade head failed after revoking the runtime role's privileges on "
        f"alembic_version:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
