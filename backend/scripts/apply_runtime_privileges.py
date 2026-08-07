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

The actual policy (which tables/functions, which exact privileges, how ownership/BYPASSRLS/
search_path are verified) lives in backend/scripts/s1a_privilege_policy.py — the SAME module
ensure_app_role.py also calls, immediately after its own broad GRANT ALL, whenever the S1A
objects already exist. This script exists for the complementary case: the very deploy that
creates the S1A objects for the first time, where ensure_app_role.py runs BEFORE the
migration and has nothing to narrow yet.

Runs the whole REVOKE/GRANT/verify sequence in a single transaction and commits ONLY if
verification is fully green — a crash or a policy mismatch partway through leaves the
PREVIOUS privilege state committed, never a half-narrowed one. Only runs meaningfully when
the S1A schema (migration 0019) and the mainai_app role already exist; since this script only
ever runs after `alembic upgrade head` in the real boot sequence above, a missing role/table/
function at this point is treated as a real configuration error and fails loud (raises /
SystemExit(1)), not silently skipped. The one legitimate exception is running this script
directly against a database that predates S1A (e.g. manual runs during development) — that
case is opted into explicitly via ALLOW_MISSING_S1A_SCHEMA=true, never assumed.

Also acquires `s1a_privilege_policy.acquire_privilege_boot_lock()` as the FIRST statement in
its transaction (founder-reported production incident, VPS hotfix): two concurrent callers of
this mutating path — e.g. two backend replicas briefly overlapping during a rolling deploy —
used to be able to hit Postgres's "tuple concurrently updated" on the very same REVOKE/GRANT
statements below; the advisory lock makes the second caller wait for the first's transaction
to finish instead of racing it.

--verify-only (see docker-entrypoint.sh): the durable-worker container's read-only path. Runs
ONLY the verification half of the same policy (apply_privilege_policy(..., mutate=False)) —
never a single REVOKE/GRANT — against a real Postgres READ ONLY transaction, so even a future
code path that accidentally tried to mutate here would be rejected by Postgres itself, not
just by convention. Never acquires the advisory lock (a read-only verifier must never wait
behind a writer — see acquire_privilege_boot_lock()'s own docstring). Retries with a short,
bounded backoff: this runs concurrently with (not strictly after) the container that IS
narrowing privileges for the first time on a schema-upgrading deploy, so a transient "the
policy isn't fully applied yet" read is expected and should be retried a few times, not treated
as fatal, before this container is allowed to proceed. Still fails closed (non-zero exit) if
verification never passes within the retry budget — the worker must not start polling against
an unverifiable privilege state any more than the backend may.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2  # noqa: E402

from s1a_privilege_policy import (  # noqa: E402
    APP_ROLE,
    acquire_privilege_boot_lock,
    apply_privilege_policy,
    s1a_objects_exist,
)


def _role_and_objects_exist(cur, *, allow_missing_schema: bool) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
    role_exists = cur.fetchone() is not None
    objects_exist = role_exists and s1a_objects_exist(cur)
    if objects_exist:
        return True
    if allow_missing_schema:
        print(
            "apply_runtime_privileges: S1A schema/role not present — skipping "
            "(ALLOW_MISSING_S1A_SCHEMA=true)."
        )
        return False
    raise RuntimeError(
        "apply_runtime_privileges: S1A schema (migration 0019) or the mainai_app "
        "role is expected to already exist at this point in the boot sequence, but "
        "doesn't. Refusing to boot with an unverifiable privilege state. Set "
        "ALLOW_MISSING_S1A_SCHEMA=true to explicitly opt into skipping this (e.g. "
        "deliberate manual runs against a pre-S1A database)."
    )


def apply_and_verify(database_url: str, *, allow_missing_schema: bool = False) -> None:
    """The mutating path — see module docstring. Exactly one container (the backend) should
    ever call this."""
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    committed = False
    try:
        with conn.cursor() as cur:
            acquire_privilege_boot_lock(cur)

            if not _role_and_objects_exist(cur, allow_missing_schema=allow_missing_schema):
                return

            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()

            # require_complete=True (the default, explicit here for clarity): run only after
            # `alembic upgrade head`, every managed table/function MUST exist by this point —
            # see s1a_privilege_policy.py's module docstring for the contrast with
            # ensure_app_role.py's require_complete=False, every-boot call.
            errors = apply_privilege_policy(cur, expected_owner=expected_owner, require_complete=True)
            if errors:
                print("apply_runtime_privileges: privilege state does NOT match policy:", file=sys.stderr)
                for err in errors:
                    print(f"  - {err}", file=sys.stderr)
                raise SystemExit(1)

        conn.commit()
        committed = True
    finally:
        if not committed:
            conn.rollback()
        conn.close()

    print("apply_runtime_privileges: privilege state verified correct.")


def _verify_once_readonly(database_url: str, *, allow_missing_schema: bool) -> list[str] | None:
    """One read-only verification attempt. Returns the error list from
    `apply_privilege_policy(..., mutate=False)` (empty means verified clean), or None if the
    S1A schema/role genuinely isn't present yet and ALLOW_MISSING_S1A_SCHEMA=true opted into
    skipping — never mutates, never acquires the advisory lock."""
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            if not _role_and_objects_exist(cur, allow_missing_schema=allow_missing_schema):
                return None
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            return apply_privilege_policy(cur, expected_owner=expected_owner, require_complete=True, mutate=False)
    finally:
        conn.rollback()  # a READ ONLY transaction never has anything to commit
        conn.close()


def verify_only(
    database_url: str,
    *,
    allow_missing_schema: bool = False,
    attempts: int = 8,
    delay_seconds: float = 2.0,
) -> None:
    """The durable-worker container's read-only path — see module docstring. Fails closed
    (SystemExit(1)) if verification doesn't pass within `attempts` tries, each `delay_seconds`
    apart (defaults: ~16s total, bounded — never an unbounded retry loop)."""
    last_errors: list[str] = []
    for attempt in range(1, attempts + 1):
        errors = _verify_once_readonly(database_url, allow_missing_schema=allow_missing_schema)
        if errors is None:
            return  # schema/role genuinely absent, explicitly allowed
        if not errors:
            print(f"apply_runtime_privileges --verify-only: privilege state verified correct (attempt {attempt}/{attempts}).")
            return
        last_errors = errors
        if attempt < attempts:
            print(
                f"apply_runtime_privileges --verify-only: privilege state not yet correct "
                f"(attempt {attempt}/{attempts}) — retrying in {delay_seconds:.1f}s.",
                file=sys.stderr,
            )
            time.sleep(delay_seconds)

    print("apply_runtime_privileges --verify-only: privilege state does NOT match policy after all retries:", file=sys.stderr)
    for err in last_errors:
        print(f"  - {err}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    allow_missing_schema = os.environ.get("ALLOW_MISSING_S1A_SCHEMA", "false").lower() == "true"
    if "--verify-only" in sys.argv[1:]:
        verify_only(database_url, allow_missing_schema=allow_missing_schema)
    else:
        apply_and_verify(database_url, allow_missing_schema=allow_missing_schema)


if __name__ == "__main__":
    main()
