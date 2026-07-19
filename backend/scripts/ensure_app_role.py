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
"""

import os
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

APP_ROLE = "mainai_app"


def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    app_password = os.environ["MAINAI_APP_PASSWORD"]
    env_file = os.environ.get("RENDER_ENV_FILE")

    parts = urlsplit(database_url)
    if not parts.hostname or not parts.username:
        print(f"DATABASE_URL saknar host eller användarnamn: {database_url!r}", file=sys.stderr)
        sys.exit(1)

    app_netloc = f"{APP_ROLE}:{quote(app_password, safe='')}@{parts.hostname}:{parts.port or 5432}"
    app_database_url = urlunsplit((parts.scheme, app_netloc, parts.path, "", ""))

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
            cur.execute(
                sql.SQL(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {role}) THEN
                        CREATE ROLE {role_ident} LOGIN PASSWORD {password};
                      ELSE
                        ALTER ROLE {role_ident} LOGIN PASSWORD {password};
                      END IF;
                    END
                    $$;
                    """
                ).format(
                    role=sql.Literal(APP_ROLE),
                    role_ident=sql.Identifier(APP_ROLE),
                    password=sql.Literal(app_password),
                )
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

    if env_file:
        with open(env_file, "a") as f:
            f.write(f'export APP_DATABASE_URL="{app_database_url}"\n')

    print(f"{APP_ROLE}-rollen är skapad/uppdaterad.")


if __name__ == "__main__":
    main()
