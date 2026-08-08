"""Idempotently provisions the restricted `mainai_app` Postgres role used for all runtime
request handling (see docs/OPERATIONS.md, docs/RENDER_DEPLOY.md and
backend/db-init/01-app-role.sh, which does the same thing for local Docker Compose via a
Postgres `docker-entrypoint-initdb.d` init script).

Managed Postgres providers (e.g. Render) don't let you mount an init script into their
Postgres container, so this is the equivalent for those: connect with the admin/owner role
(DATABASE_URL), create-or-update `mainai_app` with the given password, and grant it the same
privileges the init script grants. Run once per boot, before `alembic upgrade head` (see
backend/docker-entrypoint.sh) — safe to repeat on every deploy.

Only invoked when MAINAI_APP_PASSWORD is set (Render Blueprint provides it as a
platform-generated secret, never committed to the repo). Local Docker Compose already sets
APP_DATABASE_URL directly and never sets MAINAI_APP_PASSWORD on the backend container, so
docker-entrypoint.sh skips this there and nothing changes for local dev.

On success, appends `export APP_DATABASE_URL=...` to the file named by $RENDER_ENV_FILE so
the calling shell can source it before starting the app — this script's own process exiting
can't otherwise change its parent shell's environment.

Default-privilege grants below are scoped `FOR ROLE {current_user}` (the role THIS connection
actually authenticated as — see the comment at that call site), not the role named in
DATABASE_URL's URL, which under a connection pooler (e.g. Supabase's Session pooler) can be a
login identity rather than a real role. This relies on one assumption: that
backend/docker-entrypoint.sh runs this script and `alembic upgrade head` with the exact same
DATABASE_URL, in the same boot — so the role Alembic's later DDL runs as is the same role this
script just granted default privileges for. If a future change ever pointed those two steps at
different connection strings, this guarantee would silently stop holding.

Password rotation is deliberate, not a boot side effect (verified production incident,
2026-07-20): the previous version of this script ran `ALTER ROLE mainai_app LOGIN PASSWORD
...` unconditionally on EVERY boot, even when the role's password was already correct. Under
Supabase's Session Pooler (Supavisor), that ALTER ROLE can leave Supavisor's own auth cache
briefly stale: a connection attempt as mainai_app moments later, with the SAME (correct,
freshly-set) password, was observed to fail with "password authentication failed for user
mainai_app", then succeed outright on the very next boot with no code or credential change at
all. Rotating the password on every single restart meant paying that propagation-lag risk on
every single restart, forever, even though the password was already correct almost every
time. Now: only a role that's freshly created, or an explicit MAINAI_APP_ROTATE_PASSWORD=true,
actually changes the password; a normal restart against an already-correctly-provisioned role
never touches it. Whenever the password genuinely does change, this script proves the new
credential actually works through the pooler — retrying with backoff — before reporting
success, instead of leaving that risk for app/main.py's own startup path (which, before this
fix, had no such retry and took the whole process down on the first transient rejection).

The broad table grant below runs on EVERY boot, including an ordinary restart on a database
that's already past migration 0019 (S1A). Pass 30 narrowed it from `ALL PRIVILEGES` to
`SELECT, INSERT, UPDATE, DELETE` (see that call site, and `_NEVER_GRANTED_TABLE_PRIVS` in
s1a_privilege_policy.py: `ALL` also means TRUNCATE/REFERENCES/TRIGGER, none of which any
runtime path uses, and TRUNCATE is not subject to RLS at all). It is still a broad grant
across every table in the schema, and would still re-widen mainai_app's carefully narrowed
privileges on the S1A tables/functions
right back open, if only for the brief window until apply_runtime_privileges.py next runs
(which, on a mid-deploy crash between this script and that one, might be never). This script
therefore re-applies the SAME S1A privilege policy (backend/scripts/s1a_privilege_policy.py)
immediately afterward, in the SAME transaction, whenever the S1A objects already exist — the
transaction only commits if that re-narrowing verifies clean, so the wide-open GRANT above is
never the durable, committed state for those specific tables. On a boot where this script
runs BEFORE `alembic upgrade head` first creates the S1A tables, there's nothing to narrow
yet here; apply_runtime_privileges.py (run after the migration, see docker-entrypoint.sh)
applies the same policy for the first time in that case.

--derive-only (see docker-entrypoint.sh): the durable-worker container's path. Computes and
writes `APP_DATABASE_URL` to $RENDER_ENV_FILE exactly as the full path below does, but touches
NO database at all — no connection, no CREATE/ALTER ROLE, no GRANT, no privilege re-narrowing.
Safe to run on every container unconditionally: deriving the connection string is pure string
substitution from DATABASE_URL + MAINAI_APP_PASSWORD, identical on every container since both
share the same admin DATABASE_URL and the same MAINAI_APP_PASSWORD. Founder-reported
production incident (VPS hotfix): before this mode existed, the worker container ran the FULL
mutating path unconditionally too (see docker-entrypoint.sh's history) — including its own
GRANT ALL + S1A re-narrow transaction, in parallel with the backend container's own identical
transaction, occasionally colliding on Postgres's "tuple concurrently updated" for the exact
same catalog rows. Only the backend now ever runs the mutating path (also now behind
`acquire_privilege_boot_lock()`, defending the remaining case of two backend replicas racing
each other — see that function's docstring).
"""

import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402

from s1a_privilege_policy import acquire_privilege_boot_lock, apply_privilege_policy  # noqa: E402

APP_ROLE = "mainai_app"

# Test-only hook — never set in a real deployment. Lets the combined-container-verify CI job
# (see .github/workflows/ci.yml) deterministically exercise the retry/backoff path below
# against the real image without depending on Supavisor's actual (external, non-deterministic)
# auth-cache timing. Consumed by _self_test_connection() below.
_TEST_FORCE_CONNECT_FAILURES = int(os.environ.get("TEST_FORCE_APP_DB_CONNECT_FAILURES", "0") or "0")


def _app_username(admin_username: str) -> str:
    """Under Supabase's Session Pooler (Supavisor), the admin URL's username is a pooler
    login identity of the form `<role>.<tenant-id>` (see the current_user comment below) —
    the `.<tenant-id>` suffix is how Supavisor knows which project's Postgres to route the
    connection to, and every connection through the pooler needs it, not just the admin one.
    Building the app role's connection string as bare "mainai_app" (no suffix) makes Supavisor
    reject it outright with "no tenant identifier provided (external_id or sni_hostname
    required)" — a production crash, confirmed against a real Render deploy, not a
    hypothetical. A plain, non-pooled admin username (local dev, direct Postgres) has no dot
    and is left alone."""
    if "." in admin_username:
        _, _, tenant = admin_username.partition(".")
        return f"{APP_ROLE}.{tenant}"
    return APP_ROLE


def _self_test_connection(app_database_url: str, *, attempts: int = 5, base_delay_seconds: float = 1.0) -> None:
    """Proves the just-created-or-rotated mainai_app credential is actually usable through
    the pooler before this script reports success — see the module docstring for the
    production incident this reproduces and fixes. Retries with exponential backoff
    (1s/2s/4s/8s by default) on a connection-level failure; raises (failing the whole boot
    loudly, via docker-entrypoint.sh's `set -e`) if every attempt fails, since that would mean
    a genuinely wrong credential, not just pooler propagation lag."""
    remaining_simulated_failures = _TEST_FORCE_CONNECT_FAILURES
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if remaining_simulated_failures > 0:
                remaining_simulated_failures -= 1
                raise psycopg2.OperationalError(
                    f"TEST HOOK: TEST_FORCE_APP_DB_CONNECT_FAILURES simulated failure "
                    f"({_TEST_FORCE_CONNECT_FAILURES - remaining_simulated_failures}/{_TEST_FORCE_CONNECT_FAILURES})"
                )
            test_conn = psycopg2.connect(app_database_url, connect_timeout=5)
            test_conn.close()
            if attempt > 1:
                print(f"{APP_ROLE}-anslutningen fungerar (försök {attempt}/{attempts}).")
            return
        except psycopg2.OperationalError as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay_seconds * (2 ** (attempt - 1))
            print(
                f"{APP_ROLE}-anslutningen misslyckades (försök {attempt}/{attempts}): {exc} "
                f"— försöker igen om {delay:.1f}s.",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise RuntimeError(f"Kunde inte ansluta som {APP_ROLE} efter {attempts} försök.") from last_exc


def _derive_app_database_url(database_url: str, app_password: str) -> str:
    """Pure computation, no I/O: the exact same `APP_DATABASE_URL` the full mutating path
    below derives, from DATABASE_URL + MAINAI_APP_PASSWORD alone. Shared by both `main()`'s
    full path and `--derive-only` so the two can never silently diverge."""
    parts = urlsplit(database_url)
    if not parts.hostname or not parts.username:
        print(f"DATABASE_URL saknar host eller användarnamn: {database_url!r}", file=sys.stderr)
        sys.exit(1)
    app_username = _app_username(parts.username)
    app_netloc = f"{quote(app_username, safe='.')}:{quote(app_password, safe='')}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit((parts.scheme, app_netloc, parts.path, "", ""))


def derive_only() -> None:
    """--derive-only: the durable-worker container's path — see module docstring. Computes
    APP_DATABASE_URL and writes it to $RENDER_ENV_FILE, touching no database at all."""
    database_url = os.environ["DATABASE_URL"]
    app_password = os.environ["MAINAI_APP_PASSWORD"]
    env_file = os.environ.get("RENDER_ENV_FILE")

    app_database_url = _derive_app_database_url(database_url, app_password)

    if env_file:
        with open(env_file, "a") as f:
            f.write(f'export APP_DATABASE_URL="{app_database_url}"\n')

    print(f"{APP_ROLE}: APP_DATABASE_URL härledd (--derive-only, ingen databasanslutning gjord).")


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    app_password = os.environ["MAINAI_APP_PASSWORD"]
    env_file = os.environ.get("RENDER_ENV_FILE")
    # Explicit, one-off opt-in for an operator who actually wants to rotate the password on
    # this specific deploy — never inferred, never a side effect of an ordinary restart.
    rotate_password = os.environ.get("MAINAI_APP_ROTATE_PASSWORD", "false").lower() == "true"

    app_database_url = _derive_app_database_url(database_url, app_password)

    password_changed = False

    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    committed = False
    try:
        with conn.cursor() as cur:
            # Founder-reported production incident (VPS hotfix): see
            # s1a_privilege_policy.acquire_privilege_boot_lock()'s own docstring for the exact
            # "tuple concurrently updated" mechanism this closes. Must be the FIRST statement
            # in this transaction, before anything below touches a catalog row this policy
            # manages.
            acquire_privilege_boot_lock(cur)

            # The connected role, not DATABASE_URL's username. Under Supabase's Session
            # Pooler (required on Render for IPv4 reachability — see docs/RENDER_DEPLOY.md),
            # the URL's username is a pooler login identity of the form
            # `postgres.<project-ref>`, not a real Postgres role; the pooler maps it to the
            # actual role (typically `postgres`) for the session. `ALTER DEFAULT PRIVILEGES
            # FOR ROLE postgres.<project-ref>` then fails with "role ... does not exist"
            # because no such role exists in pg_roles. Ask Postgres what it actually
            # authenticated the session as instead of assuming the URL username is a role.
            cur.execute("SELECT current_user")
            (admin_role,) = cur.fetchone()

            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            role_exists = cur.fetchone() is not None

            if not role_exists:
                cur.execute(
                    sql.SQL("CREATE ROLE {role_ident} LOGIN PASSWORD {password}").format(
                        role_ident=sql.Identifier(APP_ROLE), password=sql.Literal(app_password)
                    )
                )
                password_changed = True
                print(f"{APP_ROLE}-rollen skapades (nytt lösenord satt).")
            elif rotate_password:
                cur.execute(
                    sql.SQL("ALTER ROLE {role_ident} LOGIN PASSWORD {password}").format(
                        role_ident=sql.Identifier(APP_ROLE), password=sql.Literal(app_password)
                    )
                )
                password_changed = True
                print(f"{APP_ROLE}-rollen finns redan — lösenordet roterat (MAINAI_APP_ROTATE_PASSWORD=true).")
            else:
                # Deliberately NOT altering the password on a normal boot — see the module
                # docstring on why an unconditional ALTER ROLE here was a real production
                # incident, not a hypothetical.
                print(
                    f"{APP_ROLE}-rollen finns redan — lösenordet ändras INTE (sätt "
                    "MAINAI_APP_ROTATE_PASSWORD=true för att rotera det explicit)."
                )

            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(APP_ROLE))
            )
            # Pass 30: `SELECT, INSERT, UPDATE, DELETE`, never `ALL PRIVILEGES`. `ALL` on a
            # table also means TRUNCATE, REFERENCES and TRIGGER — none of which any runtime
            # code path uses, and TRUNCATE specifically is NOT subject to Row-Level Security
            # (see `_NEVER_GRANTED_TABLE_PRIVS` in s1a_privilege_policy.py for the full
            # finding). Narrowing the grant here is only half the fix and cannot stand alone:
            # a database that has booted even once already has the wide `arwdDxt` ACL durably
            # committed, and a GRANT never removes privileges. The policy call below is what
            # actually REVOKEs them, on this and every future boot.
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(
                    sql.Identifier(APP_ROLE)
                )
            )
            cur.execute(
                sql.SQL("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                    sql.Identifier(APP_ROLE)
                )
            )
            # So tables/sequences created by later `alembic upgrade head` runs (through the
            # admin role) are automatically granted to mainai_app too — matching
            # db-init/01-app-role.sh's behavior for local Docker Compose.
            # Same narrowing as the direct grant above, for tables that don't exist yet. The
            # matching `ALTER DEFAULT PRIVILEGES ... REVOKE TRUNCATE, REFERENCES, TRIGGER` that
            # clears the legacy `arwdDxt` default-ACL entry on an already-deployed database
            # lives in apply_privilege_policy() — see `_default_acl_privileges()` for the
            # measured proof that this narrower GRANT alone does NOT replace it.
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app}"
                ).format(admin=sql.Identifier(admin_role), app=sql.Identifier(APP_ROLE))
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
                    "GRANT ALL PRIVILEGES ON SEQUENCES TO {app}"
                ).format(admin=sql.Identifier(admin_role), app=sql.Identifier(APP_ROLE))
            )

            # Re-narrow mainai_app's privileges on the S1A tables/functions right back down,
            # in this SAME transaction, unconditionally — see the module docstring. Pass 24:
            # this used to be gated behind "every S1A object this codebase knows about
            # exists" (`s1a_objects_exist(cur)`), which opened a real mixed-version boot
            # window: a RUN_MIGRATIONS=false worker container running code that already knows
            # about a LATER migration (e.g. 0020), against a database that hasn't received
            # that migration yet, would find the gate False (that migration's object doesn't
            # exist yet) and skip narrowing ENTIRELY — including the 0019 tables/functions
            # that DO already exist and were already correctly narrowed before this boot. The
            # wide GRANT ALL above would then commit as the durable state for those objects
            # too, not just the missing one. `require_complete=False` narrows whatever subset
            # of protected tables/functions already exists, every single boot, with no such
            # gate — a legitimately-missing future object is simply skipped (nothing to leak,
            # since mainai_app never had access to an object that doesn't exist), and this
            # call is safe even on a pre-S1A database where nothing exists yet at all.
            policy_errors = apply_privilege_policy(cur, expected_owner=admin_role, require_complete=False)
            if policy_errors:
                raise RuntimeError(
                    "ensure_app_role: S1A privilege re-narrowing failed, refusing to "
                    "commit the broader GRANT above:\n" + "\n".join(f"  - {e}" for e in policy_errors)
                )

        conn.commit()
        committed = True
    finally:
        if not committed:
            conn.rollback()
        conn.close()

    if password_changed:
        _self_test_connection(app_database_url)

    if env_file:
        with open(env_file, "a") as f:
            f.write(f'export APP_DATABASE_URL="{app_database_url}"\n')

    print(f"{APP_ROLE}-rollen är klar.")


if __name__ == "__main__":
    if "--derive-only" in sys.argv[1:]:
        derive_only()
    else:
        main()
