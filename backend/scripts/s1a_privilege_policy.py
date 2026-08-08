"""Shared least-privilege policy for `mainai_app`'s runtime table/function privileges.

Two layers, applied together by the same callers, in the same transaction:

1. A SCHEMA-WIDE floor (`_NEVER_GRANTED_TABLE_PRIVS`, Pass 44): `TRUNCATE`, `REFERENCES` and
   `TRIGGER` are revoked from mainai_app on EVERY table in schema public — discovered
   dynamically from `pg_tables`, never a hardcoded list, so a table a future migration adds is
   covered the first time this runs after that migration without anyone remembering to update
   this file. See `_NEVER_GRANTED_TABLE_PRIVS`' own comment for why none of the three is
   reachable from application code, and for the PR #42 review finding that prompted it.
2. The per-table/per-function S1A memory-provenance policy (migration 0019,
   docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8) in `_PROTECTED_TABLES`/`_FUNCTIONS`, which
   narrows a handful of specific tables further than the floor (down to SELECT-only, or to no
   direct privileges at all).

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

# Founder-reported production incident (VPS deploy of PR #36's merged base): `ensure_app_role.py`
# and `apply_runtime_privileges.py` both run this policy's REVOKE/GRANT statements against the
# SAME catalog rows (pg_class.relacl, pg_proc.proacl) on every container boot — and before this
# fix, `backend/docker-entrypoint.sh` ran BOTH scripts unconditionally on every container that
# shares the image, including the durable-worker container. Two concurrent transactions each
# issuing REVOKE/GRANT against the same table/function can hit Postgres's
# "tuple concurrently updated" error (a real, observed production failure — the worker
# crash-looped on it) rather than cleanly blocking, because GRANT/REVOKE's catalog UPDATE
# doesn't queue behind a session-level lock the way ordinary DML does.
#
# `acquire_privilege_boot_lock(cur)` below serializes every mutating call into this module via
# a single, well-known Postgres advisory lock — `pg_advisory_xact_lock` blocks (rather than
# erroring) until any other transaction currently mutating S1A/mainai_job_* privileges commits
# or rolls back, and is automatically released at the end of the CALLING transaction (commit,
# rollback, or connection loss) with no separate unlock call needed. This is defense in depth
# for the case docker-entrypoint.sh's own fix (worker no longer runs the mutating path at all —
# see RUN_PRIVILEGE_BOOT there) doesn't cover: multiple BACKEND replicas (or an old+new backend
# briefly overlapping during a rolling deploy) still both running the real, mutating path.
#
# The two integers are arbitrary but fixed and documented (never derived from a hash, which
# would risk an unintended collision with some other advisory lock this codebase or a future
# one takes) — "LifeAI privilege boot", chosen once and never reused for anything else.
PRIVILEGE_BOOT_ADVISORY_LOCK_KEY = (72197001, 1)


def acquire_privilege_boot_lock(cur) -> None:
    """Blocks until this transaction holds the exclusive privilege-boot advisory lock — call
    this as the FIRST statement in any transaction that calls `apply_privilege_policy(...,
    mutate=True)` (the default), before that transaction touches any catalog row this policy
    manages. Never call this from a `mutate=False` (read-only, verification-only) caller: a
    read-only transaction should never need to wait behind a writer, and PRIVILEGE_VERIFY_ONLY
    callers (see apply_runtime_privileges.py) must stay lock-free by design so a stuck writer
    can never make a read-only verifier hang too."""
    cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", PRIVILEGE_BOOT_ADVISORY_LOCK_KEY)

# (table, [privileges mainai_app should end up with]) — everything else (UPDATE, DELETE,
# TRUNCATE, REFERENCES, TRIGGER) is revoked. lifecycle_events is SELECT-only: it's an
# append-only audit trail written exclusively by the SECURITY DEFINER functions below, never
# directly by the app role.
#
# storage_deletion_tasks (migration 0021, account erasure) — Pass 28, tightened AGAIN after a
# second founder review: mainai_app gets ZERO direct privileges (Pass 27 had left INSERT,
# reasoned as harmless metadata access — a founder review correctly pointed out that INSERT
# into THIS table is not mere metadata: it is indirect access to a privileged PHYSICAL DELETE
# operation, since nothing previously verified an inserted storage_key actually belonged to
# the inserting owner or referenced anything real at all. A compromised request path or a
# future SQL bug could have queued an arbitrary key — including one belonging to
# app/project_memory.py's founder-wide project-memory blobs, which migration 0020's reference
# check (Documents/ImportJobs only) would never see, and the maintenance worker would then
# physically delete it, believing it unreferenced. The ONLY way to create a row now is
# `enqueue_account_erasure_storage_task()` (migration 0022) — a SECURITY DEFINER function that
# verifies the caller (`app.current_user_id`) genuinely owns the storage_key via
# `Document.storage_key`/`ImportJob.source_storage_key` before ever inserting anything, and
# sets reason/status itself (never caller-supplied). Reading/claiming/updating pending or
# failed tasks — both the immediate best-effort attempt right after an erasure commits, AND
# app/worker.py's cross-operation retry scan — runs exclusively on a separate, privileged
# maintenance session bound to the admin/migration connection (mirroring app/worker.py's
# existing `_ClaimSession` pattern for `knowledge_import_jobs`).
_PROTECTED_TABLES = [
    ("memory_source_units", ["SELECT", "INSERT"]),
    ("document_source_units", ["SELECT", "INSERT"]),
    ("memory_source_lifecycle_events", ["SELECT"]),
    ("storage_deletion_tasks", []),
]

_ALL_TABLE_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]

# Pass 44 — the schema-wide privilege floor. mainai_app must never hold ANY of these on ANY
# table in schema public, whether or not that table appears in `_PROTECTED_TABLES` above.
#
# Origin: PR #42 (owner-scoped RLS on `messages`, migration 0031) shipped with a deliberately
# deferred, non-blocking finding from its independent security review — mainai_app held
# TRUNCATE on `messages`, and **Postgres RLS does not apply to TRUNCATE**. TRUNCATE is a
# whole-table operation, not a row-scoped one: no `USING`/`WITH CHECK` clause is ever
# consulted, so migration 0031's owner-scoping (and every other RLS policy in app/rls.py)
# is simply not in the code path. A single `TRUNCATE messages` from a compromised request
# path would have destroyed every owner's messages at once, with RLS fully enabled and
# FORCEd, and no policy violated. The same review verified the finding was not specific to
# `messages`: the identical blanket grant existed on `conversations`, `documents`,
# `document_chunks` and every other table, as a project-wide consequence of the
# `GRANT ALL PRIVILEGES ON ALL TABLES` pattern in ensure_app_role.py / db-init/01-app-role.sh.
# Hence a floor over every table rather than a fix for one.
#
# Why all three are safe to remove — none is reachable from application code, and each was
# granted only as collateral of `GRANT ALL`, never because a code path asked for it:
#
# - TRUNCATE: nothing in backend/app/ or backend/scripts/ issues one. Every bulk row removal
#   the application performs is a row-scoped `DELETE` through SQLAlchemy (e.g. account
#   erasure's `db.query(Message).filter(...).delete()` in app/rag/account_erasure.py, and
#   `delete_document_chunks()` in app/rag/vector_store.py) — which stays subject to RLS,
#   which is exactly the property that makes it safe and TRUNCATE unsafe. The one place the
#   codebase truncates at all is the test fixture `_clean_tables` in backend/tests/conftest.py,
#   and that runs on the ADMIN/migration connection (`settings.database_url`), never as
#   mainai_app — so removing this does not affect the test suite either.
# - REFERENCES: only ever needed to CREATE a foreign key constraint pointing AT the table.
#   That is DDL, performed exclusively by Alembic through the admin/migration role (see
#   backend/docker-entrypoint.sh's boot order); the runtime role additionally has no CREATE on
#   schema public (revoked below), so it cannot create a table to reference from in the first
#   place.
# - TRIGGER: only ever needed to CREATE TRIGGER on the table. Also DDL, also Alembic-only —
#   and actively dangerous to leave granted, because it is the one privilege here that lets a
#   non-owner attach new behaviour to a table it does not own. Firing an EXISTING trigger
#   never requires the DML-issuing role to hold TRIGGER (Postgres invokes trigger functions as
#   part of the DML statement itself — the same reasoning already documented for
#   `mainai_app_execute=False` in app/rls.py's `_MAINAI_JOB_FUNCTION_SPECS`), so revoking it
#   cannot affect migration 0030's `messages_assign_sequence_number` sequencing triggers or
#   migration 0019's `trg_msu_no_delete` guards.
#
# NOT included here, deliberately: SELECT/INSERT/UPDATE/DELETE. Those are genuinely used
# across the user-data tables (messages alone needs all four — SELECT+INSERT in
# app/routers/chat.py, UPDATE in app/rag/message_sequence_backfill.py's
# `UPDATE messages m SET sequence_number = ...`, DELETE in app/rag/account_erasure.py), they
# stay row-scoped under RLS, and narrowing them further is a per-table judgement that belongs
# in `_PROTECTED_TABLES` with its own evidence — not in a blanket floor.
_NEVER_GRANTED_TABLE_PRIVS = ["TRUNCATE", "REFERENCES", "TRIGGER"]

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
#
# enqueue_account_erasure_storage_task (migration 0022, Pass 28): owner-scoped, same
# reasoning as transition_own_memory_source/erase_owner_memory above — it only ever touches
# rows matching the caller's own id (verified explicitly in the function body, not via RLS),
# so it needs no BYPASSRLS. mainai_app DOES need EXECUTE: it's the only way an ordinary
# request session may create a storage_deletion_tasks row at all now that table itself grants
# nothing directly (see _PROTECTED_TABLES above).
_FUNCTIONS = [
    ("transition_own_memory_source", True, False, "void", ("uuid", "varchar", "text")),
    ("transition_memory_source_admin", False, True, "void", ("uuid", "varchar", "text", "varchar", "uuid")),
    ("erase_owner_memory", True, False, "void", ("uuid",)),
    ("erase_owner_memory_admin", False, True, "void", ("uuid",)),
    ("storage_key_still_referenced_global", True, True, "boolean", ("text",)),
    ("enqueue_account_erasure_storage_task", True, False, "void", ("uuid", "text")),
]


def _table_exists(cur, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table_name}",))
    return bool(cur.fetchone()[0])


def _all_public_tables(cur) -> list[str]:
    """Every ordinary table in schema public, read from the catalog at call time rather than
    kept as a list in this file — the schema-wide floor must cover tables that migrations
    added after this module was last edited, including `alembic_version` itself."""
    cur.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [r[0] for r in cur.fetchall()]


def _default_acl_privileges(cur) -> set[str]:
    """The privileges mainai_app is set to receive AUTOMATICALLY on tables created in schema
    public from now on, read from `pg_default_acl` (the stored `ALTER DEFAULT PRIVILEGES`
    state), not from any GRANT statement's text.

    This is the half of the fix that is easy to get wrong, and was verified against a real
    Postgres 16 rather than assumed: `ALTER DEFAULT PRIVILEGES ... GRANT` is ADDITIVE, not a
    replacement. Re-issuing the historical `GRANT ALL PRIVILEGES ON TABLES` as a narrower
    `GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES` leaves the stored ACL entry at the full
    `mainai_app=arwdDxt/<admin>` — measurably unchanged — so every table a FUTURE migration
    creates would still arrive with TRUNCATE/REFERENCES/TRIGGER attached, silently undoing the
    floor one migration later. Only an explicit `ALTER DEFAULT PRIVILEGES ... REVOKE TRUNCATE,
    REFERENCES, TRIGGER ON TABLES FROM mainai_app` actually clears those bits (to
    `mainai_app=arwd/<admin>`). An empty result here is correct and safe on a database where
    no default-privilege grant was ever made — absence means new tables get nothing extra.

    `aclexplode` is used rather than string-matching the `defaclacl` text so this reads the
    same structured privilege names Postgres itself uses."""
    cur.execute(
        "SELECT DISTINCT a.privilege_type "
        "FROM pg_default_acl d "
        "JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
        "WHERE n.nspname = 'public' AND d.defaclobjtype = 'r' "
        "  AND a.grantee = %s::regrole::oid",
        (APP_ROLE,),
    )
    return {r[0] for r in cur.fetchall()}


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


def apply_privilege_policy(
    cur, *, expected_owner: str, require_complete: bool = True, mutate: bool = True
) -> list[str]:
    """Applies deny-by-default/explicit-allow GRANT/REVOKE for mainai_app on the S1A
    tables/functions and the public schema's CREATE privilege, then re-verifies with real
    privilege/catalog queries. Returns a list of human-readable error strings (empty if
    everything matches policy exactly) — never raises for a policy mismatch itself, and
    never commits/rolls back; the caller's transaction does that based on whether this
    returns any errors.

    `mutate` (default True): when False, every REVOKE/GRANT statement below is skipped
    entirely — only the verification queries run, against whatever privilege state already
    exists. This is the durable-worker container's read-only path (see
    apply_runtime_privileges.py's `--verify-only`): the worker must never itself narrow or
    widen mainai_app's privileges (that's the backend's job, exactly once), only confirm the
    backend already left them correct, and fail closed (non-zero exit) if not. A caller with
    `mutate=False` must NOT call `acquire_privilege_boot_lock()` first — a read-only verifier
    must never block behind a writer's lock (see that function's own docstring).

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

    if mutate:
        # --- Pass 44: the schema-wide floor, applied BEFORE the per-table S1A narrowing below
        # (which is strictly tighter still, so the two never fight). Covers every table in
        # public, including ones no migration in this repo has introduced yet.
        never_granted_sql = ", ".join(_NEVER_GRANTED_TABLE_PRIVS)
        for table in _all_public_tables(cur):
            cur.execute(f'REVOKE {never_granted_sql} ON TABLE public."{table}" FROM {APP_ROLE}')

        # Stop the SAME privileges from coming back automatically on tables that don't exist
        # yet — see `_default_acl_privileges()` for why a narrower GRANT alone is measurably
        # insufficient and this explicit REVOKE is required. Scoped `FOR ROLE {expected_owner}`
        # (the role this connection actually authenticated as) for the same pooler reason
        # ensure_app_role.py documents at its own ALTER DEFAULT PRIVILEGES call sites.
        cur.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{expected_owner}" IN SCHEMA public '
            f"REVOKE {never_granted_sql} ON TABLES FROM {APP_ROLE}"
        )

        for table, allowed_privs in _PROTECTED_TABLES:
            if table not in tables_present:
                continue
            cur.execute(f'REVOKE ALL ON TABLE public."{table}" FROM {APP_ROLE}')
            # Pass 28: storage_deletion_tasks' allowed_privs is deliberately empty (zero direct
            # privileges) -- `GRANT  ON TABLE ... TO role` (an empty privilege list) is invalid
            # SQL, and REVOKE ALL above already leaves the role with nothing, so there is simply
            # no GRANT statement to issue in that case.
            if allowed_privs:
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

    # Pass 44, the schema-wide floor. Runs in BOTH mutate and read-only (`mutate=False`,
    # durable-worker `--verify-only`) modes: the worker must be able to fail closed on a
    # database where TRUNCATE is still held, exactly as it already does for the S1A tables.
    # `has_table_privilege` rather than information_schema.role_table_grants, for the same
    # reason app/rls.py's `_effective_table_privileges` gives: the information_schema view
    # only lists grants made DIRECTLY to the named grantee, so a privilege held INDIRECTLY via
    # membership in another granted role would be invisible there while still letting
    # mainai_app actually run the statement. has_table_privilege computes Postgres's real,
    # effective answer, following the role-membership graph the way the server itself does.
    for table in _all_public_tables(cur):
        for priv in _NEVER_GRANTED_TABLE_PRIVS:
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (APP_ROLE, f'public."{table}"', priv))
            if cur.fetchone()[0]:
                errors.append(
                    f"{table}.{priv}: {APP_ROLE} still holds it — no table in schema public may "
                    f"grant {APP_ROLE} any of {_NEVER_GRANTED_TABLE_PRIVS} (RLS does not apply to "
                    f"TRUNCATE, and REFERENCES/TRIGGER are DDL-only privileges the runtime role "
                    f"never uses)"
                )

    leaked_defaults = _default_acl_privileges(cur) & set(_NEVER_GRANTED_TABLE_PRIVS)
    if leaked_defaults:
        errors.append(
            f"default privileges: tables created in schema public from now on would still grant "
            f"{APP_ROLE} {sorted(leaked_defaults)} automatically (pg_default_acl) — the "
            f"schema-wide floor would be silently undone by the next migration that adds a table"
        )

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
