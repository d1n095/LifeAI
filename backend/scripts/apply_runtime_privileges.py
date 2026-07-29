"""Boot-persistent privilege hardening for the S1A memory-provenance tables (docs/
MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, migration 0019).

Why this exists as a SEPARATE script from the migration itself: `ensure_app_role.py` runs
BEFORE `alembic upgrade head` on every single boot (see backend/docker-entrypoint.sh) and
unconditionally re-grants `ALL PRIVILEGES` to `mainai_app` on every table in schema public —
not just at role creation, every ordinary restart. A `REVOKE UPDATE, DELETE` written only
into migration 0019 would hold after the deploy that ships it, but get silently undone on
the very next restart, since Alembic has nothing new to apply on a normal reboot and never
re-runs that REVOKE. This script re-applies (and verifies) the same narrowed privilege state
on every boot, run AFTER `alembic upgrade head` and BEFORE the app starts serving requests:

    ensure_app_role  ->  alembic upgrade head  ->  apply_runtime_privileges  ->  start app

Idempotent and safe to run even when S1A hasn't been migrated yet (skips gracefully if the
tables/functions/role don't exist — e.g. a database that's only run migrations up to some
earlier revision, or the "Alembic migration check" CI job, which runs bare `alembic upgrade
head` directly against a database where `mainai_app` never gets created at all and where this
script is simply never invoked).

Verifies the result with real privilege queries (has_table_privilege/has_function_privilege/
has_schema_privilege) rather than assuming REVOKE/GRANT succeeded, and exits non-zero (which
backend/docker-entrypoint.sh's `set -e` turns into a failed, refused boot) if the privilege
state doesn't match policy exactly — the same "don't start half-correct" discipline
`ensure_app_role.py`'s own self-test already applies to the app role's credential.
"""

import os
import sys

import psycopg2

APP_ROLE = "mainai_app"

# (table, [privileges to revoke from mainai_app])
_PROTECTED_TABLES = [
    ("memory_source_units", ["UPDATE", "DELETE"]),
    ("document_source_units", ["UPDATE", "DELETE"]),
    ("memory_source_lifecycle_events", ["UPDATE", "DELETE", "INSERT"]),
]

# (function name, granted to mainai_app?) — matched by name alone (each name below has
# exactly one overload in this schema; see _function_signature).
_FUNCTIONS = [
    ("transition_own_memory_source", True),
    ("transition_memory_source_admin", False),
    ("erase_owner_memory", True),
    ("erase_owner_memory_admin", False),
]


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",))
    return bool(cur.fetchone()[0])


def _function_signature(cur, name: str) -> str | None:
    """Returns the function's full `name(args)` signature as Postgres identifies it (needed
    for GRANT/REVOKE/has_function_privilege, which all require the exact overload signature),
    or None if no such function exists. Matched by name alone: every function this script
    manages has exactly one overload in this schema, and pg_get_function_identity_arguments()
    includes parameter names (e.g. "p_source_id uuid, ..."), which is brittle to hardcode and
    keep in sync with the migration by hand — looking up by (schema, name) avoids that
    entirely, and raises loudly instead of silently matching nothing if that assumption
    (exactly one overload) is ever violated."""
    cur.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = %s",
        (name,),
    )
    rows = cur.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"apply_runtime_privileges: expected exactly one overload of {name}, found {len(rows)}")
    return rows[0][0] if rows else None


def apply_and_verify(database_url: str) -> None:
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    errors: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            if cur.fetchone() is None:
                print(f"{APP_ROLE}-rollen finns inte i den här databasen — hoppar över privilegiehärdning.")
                return

            any_protected_table_exists = False
            for table, revoke_privs in _PROTECTED_TABLES:
                if not _table_exists(cur, table):
                    continue
                any_protected_table_exists = True
                privs = ", ".join(revoke_privs)
                cur.execute(f'REVOKE {privs} ON "{table}" FROM {APP_ROLE}')

            if not any_protected_table_exists:
                print("Inga S1A-proveniens-tabeller finns än — hoppar över privilegiehärdning.")
                return

            signatures: dict[str, str] = {}
            for name, grant_to_app in _FUNCTIONS:
                sig = _function_signature(cur, name)
                if sig is None:
                    continue
                signatures[name] = sig
                cur.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
                if grant_to_app:
                    cur.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {APP_ROLE}")
                else:
                    cur.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {APP_ROLE}")

            cur.execute(f"REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute(f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}")

            # --- verification: real privilege queries, not an assumption ---
            for table, revoke_privs in _PROTECTED_TABLES:
                if not _table_exists(cur, table):
                    continue
                cur.execute(
                    "SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = %s", (table,)
                )
                (owner,) = cur.fetchone()
                if owner == APP_ROLE:
                    errors.append(f"{table}: owned by {APP_ROLE} (should be the admin/migration role)")
                for priv in revoke_privs:
                    cur.execute("SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, priv))
                    if cur.fetchone()[0]:
                        errors.append(f"{table}: {APP_ROLE} still has {priv}")

            for name, grant_to_app in _FUNCTIONS:
                sig = signatures.get(name)
                if sig is None:
                    continue
                cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (APP_ROLE, sig))
                actually_can_execute = bool(cur.fetchone()[0])
                if actually_can_execute != grant_to_app:
                    errors.append(
                        f"{name}: {APP_ROLE} EXECUTE = {actually_can_execute}, expected {grant_to_app}"
                    )
                cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (sig,))
                if cur.fetchone()[0]:
                    errors.append(f"{name}: PUBLIC still has EXECUTE")

            cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (APP_ROLE,))
            if cur.fetchone()[0]:
                errors.append(f"public schema: {APP_ROLE} still has CREATE")
            cur.execute("SELECT has_schema_privilege('public', 'public', 'CREATE')")
            if cur.fetchone()[0]:
                errors.append("public schema: PUBLIC still has CREATE")
    finally:
        conn.close()

    if errors:
        print("apply_runtime_privileges: privilege state does NOT match policy:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        raise SystemExit(1)

    print("apply_runtime_privileges: privilege state verified correct.")


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    apply_and_verify(database_url)


if __name__ == "__main__":
    main()
