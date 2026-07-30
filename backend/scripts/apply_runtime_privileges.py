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
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2  # noqa: E402

from s1a_privilege_policy import APP_ROLE, apply_privilege_policy, s1a_objects_exist  # noqa: E402


def apply_and_verify(database_url: str, *, allow_missing_schema: bool = False) -> None:
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    committed = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            role_exists = cur.fetchone() is not None
            objects_exist = role_exists and s1a_objects_exist(cur)

            if not objects_exist:
                if allow_missing_schema:
                    print(
                        "apply_runtime_privileges: S1A schema/role not present — skipping "
                        "(ALLOW_MISSING_S1A_SCHEMA=true)."
                    )
                    return
                raise RuntimeError(
                    "apply_runtime_privileges: S1A schema (migration 0019) or the mainai_app "
                    "role is expected to already exist at this point in the boot sequence, but "
                    "doesn't. Refusing to boot with an unverifiable privilege state. Set "
                    "ALLOW_MISSING_S1A_SCHEMA=true to explicitly opt into skipping this (e.g. "
                    "deliberate manual runs against a pre-S1A database)."
                )

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


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    allow_missing_schema = os.environ.get("ALLOW_MISSING_S1A_SCHEMA", "false").lower() == "true"
    apply_and_verify(database_url, allow_missing_schema=allow_missing_schema)


if __name__ == "__main__":
    main()
