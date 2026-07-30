"""Shared least-privilege policy for the S1A memory-provenance tables/functions (migration
0019, docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8).

Applied by TWO callers, each owning its own connection/transaction:

- ensure_app_role.py, immediately after its own broad `GRANT ALL PRIVILEGES ON ALL TABLES IN
  SCHEMA public TO mainai_app` (which runs on EVERY boot, not just role creation), in the
  SAME transaction, whenever the S1A objects already exist (an ordinary restart on a database
  that's already past migration 0019). This closes the window where that broad grant would
  otherwise be the durable, committed state for these specific tables even momentarily.
- apply_runtime_privileges.py, run once right after `alembic upgrade head` (see
  docker-entrypoint.sh) for the deploy that creates the S1A objects for the first time, and
  idempotently on every subsequent boot.

This module never opens a connection, never commits, and never rolls back — it only executes
statements on a cursor the caller hands it, inside a transaction the CALLER controls. Both
current callers commit only if `apply_privilege_policy()` returns no errors, and roll back
otherwise, so mainai_app's privileges on these tables/functions are never left in a
partially-applied state by a crash or a policy violation midway through.
"""

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
#
# storage_key_still_referenced_global (migration 0020, Pass 23): the one entry in this list
# that is BOTH granted to mainai_app AND requires BYPASSRLS — deliberately, not a mistake.
# Unlike the two owner-scoped functions above, it has no per-caller ownership check (it must
# see EVERY owner's live Document/ImportJob rows to correctly answer whether a
# content-addressed, globally-shared blob is still referenced by anyone at all — see
# app/rag/blob_references.py's module docstring for the cross-owner RLS gap this closes).
# Unlike the two admin-only functions above, mainai_app DOES need EXECUTE on it: it's called
# from an ordinary owner-scoped request (source purge, blob upload), not an admin-only path.
# It stays safe to expose to mainai_app anyway because it returns nothing but a boolean —
# no owner id, job id, or document detail ever crosses back into the calling (possibly
# unprivileged-relative-to-that-data) request.
_FUNCTIONS = [
    ("transition_own_memory_source", True, False),
    ("transition_memory_source_admin", False, True),
    ("erase_owner_memory", True, False),
    ("erase_owner_memory_admin", False, True),
    ("storage_key_still_referenced_global", True, True),
]


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",))
    return bool(cur.fetchone()[0])


def _function_signature(cur, name: str) -> str | None:
    """Returns the function's full, schema-qualified `public.name(args)` signature as
    Postgres identifies it (needed for GRANT/REVOKE/has_function_privilege, which all
    require the exact overload signature), or None if no such function exists. Matched by
    name alone: every function this module manages has exactly one overload in this schema,
    and pg_get_function_identity_arguments() includes parameter names (e.g. "p_source_id
    uuid, ..."), which is brittle to hardcode and keep in sync with the migration by hand —
    looking up by (schema, name) avoids that entirely, and raises loudly instead of silently
    matching nothing if that assumption (exactly one overload) is ever violated.

    Always schema-qualified with `public.` explicitly, regardless of the caller's own
    search_path — `regprocedure::text` only omits the schema when `public` happens to be on
    the resolving search_path, which every GRANT/REVOKE/verification statement below must
    not depend on."""
    cur.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = %s",
        (name,),
    )
    rows = cur.fetchall()
    if len(rows) > 1:
        raise RuntimeError(f"s1a_privilege_policy: expected exactly one overload of {name}, found {len(rows)}")
    if not rows:
        return None
    sig = rows[0][0]
    return sig if sig.startswith("public.") else f"public.{sig}"


def s1a_objects_exist(cur) -> bool:
    """True only if EVERY protected table and EVERY managed function exists — partial
    existence (e.g. a crash mid-migration) is never treated as "exists". Callers use this to
    decide whether `apply_privilege_policy()` is safe to run at all; it assumes this is
    already True and does not re-check."""
    for table, _ in _PROTECTED_TABLES:
        if not _table_exists(cur, table):
            return False
    for name, _, _ in _FUNCTIONS:
        if _function_signature(cur, name) is None:
            return False
    return True


def apply_privilege_policy(cur, *, expected_owner: str) -> list[str]:
    """Applies deny-by-default/explicit-allow GRANT/REVOKE for mainai_app on the S1A
    tables/functions and the public schema's CREATE privilege, then re-verifies with real
    privilege/catalog queries. Returns a list of human-readable error strings (empty if
    everything matches policy exactly) — never raises for a policy mismatch itself, and
    never commits/rolls back; the caller's transaction does that based on whether this
    returns any errors.

    `expected_owner` is the role every protected table and function must be owned by —
    callers pass `SELECT current_user` from their own connection (the actual admin/migration
    role for this boot), not a hardcoded name, since that role's identity varies by
    environment (plain `lifeos` locally, a Supabase pooler-mapped `postgres`, etc.).

    Assumes `s1a_objects_exist(cur)` is already True; callers must check that first."""
    errors: list[str] = []

    for table, allowed_privs in _PROTECTED_TABLES:
        cur.execute(f'REVOKE ALL ON TABLE public."{table}" FROM {APP_ROLE}')
        cur.execute(f'GRANT {", ".join(allowed_privs)} ON TABLE public."{table}" TO {APP_ROLE}')

    signatures: dict[str, str] = {}
    for name, grant_to_app, _requires_bypassrls in _FUNCTIONS:
        sig = _function_signature(cur, name)
        signatures[name] = sig  # type: ignore[assignment]
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
        if owner != expected_owner:
            errors.append(f"{table}: owned by {owner!r}, expected exactly {expected_owner!r}")
        for priv in _ALL_TABLE_PRIVS:
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, table, priv))
            actually_has = bool(cur.fetchone()[0])
            should_have = priv in allowed_privs
            if actually_has != should_have:
                errors.append(f"{table}.{priv}: {APP_ROLE} has={actually_has}, expected={should_have}")

    for name, grant_to_app, requires_bypassrls in _FUNCTIONS:
        sig = signatures[name]

        cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (APP_ROLE, sig))
        actually_can_execute = bool(cur.fetchone()[0])
        if actually_can_execute != grant_to_app:
            errors.append(f"{name}: {APP_ROLE} EXECUTE={actually_can_execute}, expected {grant_to_app}")
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
        if owner_name != expected_owner:
            errors.append(f"{name}: owned by {owner_name!r}, expected exactly {expected_owner!r}")
        if requires_bypassrls and not (owner_super or owner_bypassrls):
            errors.append(
                f"{name}: SECURITY DEFINER with no in-body ownership check — its owning "
                f"role {owner_name!r} must have BYPASSRLS or be superuser, but has neither "
                f"(rolsuper={owner_super}, rolbypassrls={owner_bypassrls}). Without this, the "
                f"function's own queries are silently filtered by RLS instead of operating "
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

    return errors
