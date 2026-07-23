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
"""

import os
import sys
import time
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

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


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    app_password = os.environ["MAINAI_APP_PASSWORD"]
    env_file = os.environ.get("RENDER_ENV_FILE")
    # Explicit, one-off opt-in for an operator who actually wants to rotate the password on
    # this specific deploy — never inferred, never a side effect of an ordinary restart.
    rotate_password = os.environ.get("MAINAI_APP_ROTATE_PASSWORD", "false").lower() == "true"

    parts = urlsplit(database_url)
    if not parts.hostname or not parts.username:
        print(f"DATABASE_URL saknar host eller användarnamn: {database_url!r}", file=sys.stderr)
        sys.exit(1)

    app_username = _app_username(parts.username)
    app_netloc = f"{quote(app_username, safe='.')}:{quote(app_password, safe='')}@{parts.hostname}:{parts.port or 5432}"
    app_database_url = urlunsplit((parts.scheme, app_netloc, parts.path, "", ""))

    password_changed = False

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
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
            cur.execute(
                sql.SQL("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {}").format(
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
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
                    "GRANT ALL PRIVILEGES ON TABLES TO {app}"
                ).format(admin=sql.Identifier(admin_role), app=sql.Identifier(APP_ROLE))
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {admin} IN SCHEMA public "
                    "GRANT ALL PRIVILEGES ON SEQUENCES TO {app}"
                ).format(admin=sql.Identifier(admin_role), app=sql.Identifier(APP_ROLE))
            )
    finally:
        conn.close()

    if password_changed:
        _self_test_connection(app_database_url)

    if env_file:
        with open(env_file, "a") as f:
            f.write(f'export APP_DATABASE_URL="{app_database_url}"\n')

    print(f"{APP_ROLE}-rollen är klar.")


if __name__ == "__main__":
    main()
