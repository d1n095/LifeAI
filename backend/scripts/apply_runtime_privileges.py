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

It must run unconditionally on EVERY boot of EVERY container that talks to this database —
including the durable-worker container, which sets RUN_MIGRATIONS=false and never runs
`alembic upgrade head` itself. See backend/docker-entrypoint.sh: `ensure_app_role.py`'s
unconditional re-GRANT ALL runs on that container too, so skipping this script there would
leave mainai_app's privileges wide open indefinitely after any worker-only restart.

Policy is deny-by-default, explicit-allow: every relevant privilege is REVOKEd first, then
exactly the needed ones are GRANTed back — not "REVOKE the two we thought of" (UPDATE/
DELETE). TRUNCATE, REFERENCES and TRIGGER are equally unneeded on these tables and are
equally revoked; TRUNCATE in particular is NOT subject to RLS at all, so leaving it granted
would be a real, silent bypass regardless of how tight the RLS policies are.

Only runs meaningfully when the S1A schema (migration 0019) has actually been applied. This
script does NOT gracefully skip when it can't find the `mainai_app` role or the S1A tables —
databases that reach this script are always expected to already be past migration 0019 (this
script only ever runs after `alembic upgrade head` in the real boot sequence above), so a
missing role/table/function at this point is treated as a real configuration error and fails
loud (SystemExit(1)), not silently skipped. The one legitimate case this needs to tolerate is
this script being invoked directly against a database that predates S1A (e.g. manual runs
during development) — that case is opted into explicitly via ALLOW_MISSING_S1A_SCHEMA=true,
never assumed.

Verifies the result with real privilege/catalog queries (has_table_privilege/
has_function_privilege/has_schema_privilege, pg_proc, pg_roles) rather than assuming REVOKE/
GRANT succeeded, and exits non-zero (which backend/docker-entrypoint.sh's `set -e` turns into
a failed, refused boot) if the privilege state doesn't match policy exactly — the same "don't
start half-correct" discipline `ensure_app_role.py`'s own self-test already applies to the
app role's credential.
"""

import os
import sys

import psycopg2

APP_ROLE = "mainai_app"

# (table, [privileges mainai_app should end up with]) — everything else (UPDATE, DELETE,
# TRUNCATE, REFERENCES, TRIGGER) is revoked. lifecycle_events is SELECT-only: it's an
# append-only audit trail written exclusively by the SECURITY DEFINER functions below, never
# directly by the app role.
_PROTECTED_TABLES = [
    ("memory_source_units", ["SELECT", "INSERT"]),
    ("document_source_units", ["SELECT", "INSERT"]),
    ("memory_source_lifecycle_events", ["SELECT"]),
]

_ALL_TABLE_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]

# (function name, granted to mainai_app?, requires BYPASSRLS on its owner?) — matched by
# name alone (each name below has exactly one overload in this schema; see
# _function_signature). The two owner-scoped functions enforce ownership themselves and
# don't need BYPASSRLS; the two admin/migration functions have no such check by design and
# MUST be owned by a role with real BYPASSRLS (or superuser), since `SET row_security = off`
# does not grant anything RLS itself would deny.
_FUNCTIONS = [
    ("transition_own_memory_source", True, False),
    ("transition_memory_source_admin", False, True),
    ("erase_owner_memory", True, False),
    ("erase_owner_memory_admin", False, True),
]


class _MissingS1ASchema(RuntimeError):
    pass


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


def _require(cur, condition: bool, message: str) -> None:
    if not condition:
        raise _MissingS1ASchema(message)


def apply_and_verify(database_url: str, *, allow_missing_schema: bool = False) -> None:
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    errors: list[str] = []
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
                _require(cur, cur.fetchone() is not None, f"role {APP_ROLE} does not exist")

                for table, _ in _PROTECTED_TABLES:
                    _require(cur, _table_exists(cur, table), f"table {table} does not exist")

                signatures: dict[str, str] = {}
                for name, _grant_to_app, _requires_bypassrls in _FUNCTIONS:
                    sig = _function_signature(cur, name)
                    _require(cur, sig is not None, f"function {name} does not exist")
                    signatures[name] = sig  # type: ignore[assignment]
            except _MissingS1ASchema as exc:
                if allow_missing_schema:
                    print(f"apply_runtime_privileges: S1A schema not present ({exc}) — skipping (explicitly allowed).")
                    return
                raise RuntimeError(
                    f"apply_runtime_privileges: S1A schema (migration 0019) is expected to already be applied "
                    f"at this point in the boot sequence, but it isn't: {exc}. Refusing to boot with an "
                    f"unverifiable privilege state. Set ALLOW_MISSING_S1A_SCHEMA=true to explicitly opt into "
                    f"skipping this (e.g. deliberate manual runs against a pre-S1A database)."
                ) from exc

            # --- apply: deny-by-default, explicit-allow ---
            for table, allowed_privs in _PROTECTED_TABLES:
                cur.execute(f'REVOKE ALL ON "{table}" FROM {APP_ROLE}')
                cur.execute(f'GRANT {", ".join(allowed_privs)} ON "{table}" TO {APP_ROLE}')

            for name, grant_to_app, _requires_bypassrls in _FUNCTIONS:
                sig = signatures[name]
                cur.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
                if grant_to_app:
                    cur.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {APP_ROLE}")
                else:
                    cur.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {APP_ROLE}")

            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute(f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}")

            # --- verification: real privilege/catalog queries, not an assumption ---
            for table, allowed_privs in _PROTECTED_TABLES:
                cur.execute(
                    "SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = %s", (table,)
                )
                (owner,) = cur.fetchone()
                if owner == APP_ROLE:
                    errors.append(f"{table}: owned by {APP_ROLE} (should be the admin/migration role)")
                for priv in _ALL_TABLE_PRIVS:
                    cur.execute("SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, priv))
                    actually_has = bool(cur.fetchone()[0])
                    should_have = priv in allowed_privs
                    if actually_has != should_have:
                        errors.append(
                            f"{table}: {APP_ROLE} has {priv} = {actually_has}, expected {should_have}"
                        )

            for name, grant_to_app, requires_bypassrls in _FUNCTIONS:
                sig = signatures[name]

                cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (APP_ROLE, sig))
                actually_can_execute = bool(cur.fetchone()[0])
                if actually_can_execute != grant_to_app:
                    errors.append(
                        f"{name}: {APP_ROLE} EXECUTE = {actually_can_execute}, expected {grant_to_app}"
                    )
                cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (sig,))
                if cur.fetchone()[0]:
                    errors.append(f"{name}: PUBLIC still has EXECUTE")

                cur.execute(
                    "SELECT r.rolname, r.rolsuper, r.rolbypassrls, p.proconfig "
                    "FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE p.oid = %s::regprocedure",
                    (sig,),
                )
                owner_name, owner_super, owner_bypassrls, proconfig = cur.fetchone()
                if owner_name == APP_ROLE:
                    errors.append(f"{name}: owned by {APP_ROLE} (must be owned by the admin/migration role)")
                if requires_bypassrls and not (owner_super or owner_bypassrls):
                    errors.append(
                        f"{name}: SECURITY DEFINER with no ownership check inside the function body — "
                        f"its owning role {owner_name!r} must have BYPASSRLS or be superuser, but has "
                        f"neither (rolsuper={owner_super}, rolbypassrls={owner_bypassrls}). Without this, "
                        f"the function's own queries are silently filtered by RLS instead of operating "
                        f"admin-wide, since `SET row_security = off` does not grant anything RLS denies."
                    )
                search_path_entries = [c for c in (proconfig or []) if c.startswith("search_path=")]
                if search_path_entries != ["search_path=pg_catalog"]:
                    errors.append(
                        f"{name}: search_path config is {search_path_entries!r}, expected exactly "
                        f"['search_path=pg_catalog'] (unqualified relation names inside this function "
                        f"must not be resolvable against public or a caller-created pg_temp table)"
                    )

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
    allow_missing_schema = os.environ.get("ALLOW_MISSING_S1A_SCHEMA", "false").lower() == "true"
    apply_and_verify(database_url, allow_missing_schema=allow_missing_schema)


if __name__ == "__main__":
    main()
