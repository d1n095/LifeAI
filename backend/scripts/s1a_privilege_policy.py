"""Shared least-privilege policy for the S1A memory-provenance tables/functions (migration
0019, docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8).

Applied by TWO callers, each owning its own connection/transaction, with DIFFERENT
completeness requirements (Pass 24 — see `require_complete` on `apply_privilege_policy()`):

- ensure_app_role.py, immediately after its own broad `GRANT ALL PRIVILEGES ON ALL TABLES IN
  SCHEMA public TO mainai_app` (which runs on EVERY boot, not just role creation), in the
  SAME transaction. Calls with `require_complete=False`: narrows whatever subset of protected
  tables/functions currently exists, unconditionally, every single boot — never gated on the
  FULL current-head object set existing first. A table/function a LATER, not-yet-applied
  migration will introduce is simply skipped (not an error), which is exactly what makes an
  ordinary restart mid-rollout safe: a `RUN_MIGRATIONS=false` worker container running
  already-updated code that KNOWS about a migration the database hasn't received yet must
  still narrow everything that already exists, in the SAME transaction as its own wide
  GRANT ALL — the fix for a real found bug where gating this on "every managed object exists"
  let that wide grant commit un-narrowed for the objects that already existed, simply because
  a DIFFERENT, later-migration object didn't exist yet.
- apply_runtime_privileges.py, run once right after `alembic upgrade head` (see
  docker-entrypoint.sh) — by this point in the boot sequence every migration the deployed
  code knows about should have already run. Calls with `require_complete=True` (the
  default): every table/function this policy manages MUST exist, or it's reported as an
  error and (via the caller's own commit-only-if-no-errors discipline) NOTHING is narrowed
  or granted — the database is left exactly as it was, never partially privileged.

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
#
# storage_deletion_tasks (migration 0021, account erasure) — Pass 27, tightened after a
# founder review: mainai_app gets INSERT ONLY, never SELECT/UPDATE/DELETE. This table
# deliberately has no owner_id and no RLS at all (see that migration's module docstring for
# why it must outlive the very account whose erasure created it) — a table-wide SELECT/UPDATE
# grant to the ordinary, request-scoped application role would let ANY authenticated request
# session (not just account erasure's own code path — any future bug, injected query, or
# compromised request handler reusing the same DB role) read every operation's storage keys
# and operation ids across every account's erasure, or rewrite any task's status/storage_key,
# including marking a task `purged`/`retained_shared` without ever touching the real file, or
# resetting `attempt_count`/`last_error` to sabotage the retry queue. That no router happens to
# do this today is not a security boundary — the privilege model must not depend on it.
# mainai_app's account-erasure transaction only ever needs to INSERT new rows here (see
# app/rag/account_erasure.py); reading/claiming/updating pending or failed tasks — both the
# immediate best-effort attempt right after an erasure commits, AND app/worker.py's
# cross-operation retry scan — now runs exclusively on a separate, privileged maintenance
# session bound to the admin/migration connection (mirroring app/worker.py's existing
# `_ClaimSession` pattern for `knowledge_import_jobs`, which already has the exact same
# "must see/claim rows across every owner" shape), never on the ordinary per-request session.
_PROTECTED_TABLES = [
    ("memory_source_units", ["SELECT", "INSERT"]),
    ("document_source_units", ["SELECT", "INSERT"]),
    ("memory_source_lifecycle_events", ["SELECT"]),
    ("storage_deletion_tasks", ["INSERT"]),
]

_ALL_TABLE_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]

# (function name, granted to mainai_app?, requires BYPASSRLS on its owner?, expected return
# type, expected identity argument types) — resolved by name AND exact argument types (Pass
# 25; see _resolve_function). The two owner-scoped functions enforce ownership themselves and
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
# unprivileged-relative-to-that-data) request. Pass 24: the expected-return-type check exists
# specifically because THIS function's whole safety property depends on it staying `boolean`
# — a founder review pointed out that nothing previously verified the function was even
# still SECURITY DEFINER at all, let alone that it returned only a boolean; see
# `p.prosecdef`/`prorettype` verification below.
#
# Argument types match each function's CREATE statement in the migration exactly (migration
# 0019 for the first four, 0020 for the last) — "varchar" is Postgres's own alias for
# VARCHAR(16)'s identity type `character varying` (the length modifier is not part of a
# function's identity and to_regprocedure resolves either spelling to the same signature).
_FUNCTIONS = [
    ("transition_own_memory_source", True, False, "void", ("uuid", "varchar", "text")),
    ("transition_memory_source_admin", False, True, "void", ("uuid", "varchar", "text", "varchar", "uuid")),
    ("erase_owner_memory", True, False, "void", ("uuid",)),
    ("erase_owner_memory_admin", False, True, "void", ("uuid",)),
    ("storage_key_still_referenced_global", True, True, "boolean", ("text",)),
]


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",))
    return bool(cur.fetchone()[0])


def _resolve_function(cur, name: str, expected_arg_types: tuple[str, ...]) -> tuple[str | None, list[str]]:
    """Resolves this function to its exact, schema-qualified `public.name(args)` signature,
    disambiguated by its expected identity argument types — NOT matched by name alone.

    Pass 25 found a real gap in the previous name-only lookup: a same-named function with the
    WRONG argument types (e.g. `storage_key_still_referenced_global(integer)` instead of the
    application's actual `(text)` call) would have been silently accepted as long as it was
    the only overload present and still SECURITY DEFINER, boolean-returning, correctly owned,
    and correctly granted — every check downstream of the lookup would pass while
    `blob_references.py`'s actual `SELECT public.storage_key_still_referenced_global(:key)`
    call broke at runtime, resolving to a function this policy never actually verified.

    Returns `(signature_or_None, errors)`:
    - `errors` is non-empty exactly when this name's overload set is itself a policy
      violation — either more than one overload of `name` exists in `public` at all (an
      unexpected extra overload could carry its own PUBLIC/mainai_app grants this policy
      never reviews, so it is refused rather than silently ignored), or exactly one overload
      exists but its argument types don't match `expected_arg_types` (a right-named,
      wrong-signature function is treated as a missing/wrong function, never accepted).
      When `errors` is non-empty, `signature` is always `None` — nothing is ever granted or
      revoked against an unexpected overload.
    - When `signature` is `None` and `errors` is EMPTY, the function genuinely does not exist
      at all yet (zero overloads) — the caller decides whether that's an error, based on
      `require_complete`.

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
    rows = [r[0] for r in cur.fetchall()]
    if len(rows) > 1:
        return None, [
            f"{name}: expected exactly one overload in public, found {len(rows)} "
            f"({', '.join(sorted(rows))}) — refusing to grant/revoke on any of them"
        ]
    if not rows:
        return None, []

    expected_args_sql = ", ".join(expected_arg_types)
    cur.execute("SELECT to_regprocedure(%s)::text", (f"public.{name}({expected_args_sql})",))
    (resolved,) = cur.fetchone()
    if resolved is None:
        return None, [
            f"{name}: the only overload present is {rows[0]!r}, but the application expects "
            f"public.{name}({expected_args_sql}) — treating this as a missing/wrong function, "
            f"not silently accepting whatever overload happens to exist"
        ]
    sig = resolved if resolved.startswith("public.") else f"public.{resolved}"
    return sig, []


def s1a_objects_exist(cur) -> bool:
    """True only if EVERY protected table and EVERY managed function (with its EXACT expected
    signature) exists — partial existence (e.g. a crash mid-migration), or a same-named
    function with the wrong arguments, is never treated as "exists". Callers use this to
    decide whether `apply_privilege_policy()` is safe to run at all; it assumes this is
    already True and does not re-check."""
    for table, _ in _PROTECTED_TABLES:
        if not _table_exists(cur, table):
            return False
    for name, _, _, _, expected_args in _FUNCTIONS:
        sig, errors = _resolve_function(cur, name, expected_args)
        if sig is None or errors:
            return False
    return True


def apply_privilege_policy(cur, *, expected_owner: str, require_complete: bool = True) -> list[str]:
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

    `require_complete` (Pass 24): controls what happens when a managed table or function
    doesn't exist yet.
    - `True` (the default, apply_runtime_privileges.py's use after `alembic upgrade head`):
      a missing object is itself reported as an error — every managed table/function MUST
      exist by this point in the boot sequence.
    - `False` (ensure_app_role.py's use, every boot, in the SAME transaction as its own wide
      `GRANT ALL`): a missing object is silently SKIPPED, not an error — narrows whatever
      subset of protected tables/functions already exists, right now, regardless of whether
      the full current-head object set is present yet. This is what makes an ordinary
      restart mid-rollout safe: a table/function a LATER, not-yet-applied migration will
      introduce simply isn't narrowed yet (nothing to narrow — mainai_app never had access to
      an object that doesn't exist), while everything that DOES already exist is narrowed
      immediately, in the same transaction as the wide grant that would otherwise leave it
      open. See module docstring for the exact bug this closes.

    Either way, whatever DOES exist is always narrowed+verified the same way — the only
    difference is whether a missing object also counts as a reported error."""
    errors: list[str] = []

    tables_present = {table for table, _ in _PROTECTED_TABLES if _table_exists(cur, table)}
    for table, _ in _PROTECTED_TABLES:
        if table not in tables_present and require_complete:
            errors.append(f"{table}: table does not exist (required)")

    signatures: dict[str, str] = {}
    for name, _grant_to_app, _requires_bypassrls, _expected_return_type, expected_args in _FUNCTIONS:
        sig, resolve_errors = _resolve_function(cur, name, expected_args)
        if resolve_errors:
            errors.extend(resolve_errors)
            continue
        if sig is None:
            if require_complete:
                errors.append(f"{name}: function does not exist (required)")
            continue
        signatures[name] = sig

    for table, allowed_privs in _PROTECTED_TABLES:
        if table not in tables_present:
            continue
        cur.execute(f'REVOKE ALL ON TABLE public."{table}" FROM {APP_ROLE}')
        cur.execute(f'GRANT {", ".join(allowed_privs)} ON TABLE public."{table}" TO {APP_ROLE}')

    for name, grant_to_app, _requires_bypassrls, _expected_return_type, _expected_args in _FUNCTIONS:
        sig = signatures.get(name)
        if sig is None:
            continue
        cur.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
        if grant_to_app:
            cur.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {APP_ROLE}")
        else:
            cur.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {APP_ROLE}")

    cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    cur.execute(f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}")

    # --- verification: real privilege/catalog queries, not an assumption ---
    for table, allowed_privs in _PROTECTED_TABLES:
        if table not in tables_present:
            continue  # already reported above when require_complete
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

    for name, grant_to_app, requires_bypassrls, expected_return_type, _expected_args in _FUNCTIONS:
        sig = signatures.get(name)
        if sig is None:
            continue  # already reported above (missing, or a wrong/extra overload)

        cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", (APP_ROLE, sig))
        actually_can_execute = bool(cur.fetchone()[0])
        if actually_can_execute != grant_to_app:
            errors.append(f"{name}: {APP_ROLE} EXECUTE={actually_can_execute}, expected {grant_to_app}")
        cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", (sig,))
        if cur.fetchone()[0]:
            errors.append(f"{name}: PUBLIC still has EXECUTE")

        cur.execute(
            "SELECT r.rolname, r.rolsuper, r.rolbypassrls, p.proconfig, "
            "       p.prosecdef, p.prorettype::regtype::text, l.lanname "
            "FROM pg_proc p "
            "JOIN pg_roles r ON r.oid = p.proowner "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE p.oid = %s::regprocedure",
            (sig,),
        )
        owner_name, owner_super, owner_bypassrls, proconfig, prosecdef, rettype, lang = cur.fetchone()
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
        # Pass 24: every function this policy manages is a SECURITY DEFINER function by
        # design (that's the entire reason it's on this list at all) — a founder review
        # pointed out that nothing previously verified this catalog-level flag directly.
        # `ALTER FUNCTION ... SECURITY INVOKER` would leave owner/BYPASSRLS/search_path/
        # grants all unchanged and still pass every OTHER check here, while silently making
        # the function run as its CALLER (mainai_app) again — re-subjecting its own queries
        # to mainai_app's ordinary RLS scope and reintroducing exactly the cross-owner gap
        # this whole policy exists to close.
        if not prosecdef:
            errors.append(
                f"{name}: expected SECURITY DEFINER but prosecdef=false (function is SECURITY "
                f"INVOKER) — it would run with the CALLER's privileges/RLS scope, not the "
                f"owning role's, silently defeating this function's whole purpose"
            )
        if rettype != expected_return_type:
            errors.append(f"{name}: return type is {rettype!r}, expected {expected_return_type!r}")
        if lang != "plpgsql":
            errors.append(f"{name}: language is {lang!r}, expected 'plpgsql'")
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
