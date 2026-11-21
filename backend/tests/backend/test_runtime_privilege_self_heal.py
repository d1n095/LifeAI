"""The privilege policy must repair the state it rejects, not only report it.

Found by restoring an encrypted backup into a clean database: `pg_restore --no-privileges`
recreates Postgres's default PUBLIC EXECUTE on every function, so the restored database passed
the manifest comparison and then refused to boot with "PUBLIC must never hold EXECUTE" — forever,
because the enforcement phase never issued the REVOKE it was checking for. These tests grant the
forbidden privilege deliberately and prove the next policy run removes it.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import migration_engine
from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges


def _public_execute_holders(conn, function_name: str):
    return conn.execute(
        text(
            "SELECT grantee FROM information_schema.routine_privileges "
            "WHERE routine_schema = 'public' AND routine_name = :name "
            "AND privilege_type = 'EXECUTE' AND grantee = 'PUBLIC'"
        ),
        {"name": function_name},
    ).all()


def test_job_policy_revokes_public_execute_it_forbids():
    with migration_engine.begin() as conn:
        conn.execute(text("GRANT EXECUTE ON FUNCTION erase_own_mainai_job_children() TO PUBLIC"))
        assert _public_execute_holders(conn, "erase_own_mainai_job_children"), (
            "sanity check: the deliberate PUBLIC grant should be visible"
        )

    apply_mainai_job_runtime_privileges(migration_engine)

    with migration_engine.begin() as conn:
        assert not _public_execute_holders(conn, "erase_own_mainai_job_children")


def test_execution_policy_revokes_public_execute_it_forbids():
    with migration_engine.begin() as conn:
        conn.execute(text("GRANT EXECUTE ON FUNCTION erase_own_mainai_execution_children() TO PUBLIC"))
        assert _public_execute_holders(conn, "erase_own_mainai_execution_children")

    apply_mainai_execution_privileges(migration_engine)

    with migration_engine.begin() as conn:
        assert not _public_execute_holders(conn, "erase_own_mainai_execution_children")
