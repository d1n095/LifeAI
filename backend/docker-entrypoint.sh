#!/bin/bash
# Applies pending Alembic migrations, then starts the app. Migrations run through
# DATABASE_URL (the superuser/migration role — see app/db.py and app/config.py); the app
# itself serves traffic through the separate, restricted APP_DATABASE_URL role. Schema is
# never created or altered by the app at request-serving startup (see app/main.py) — this
# is the one and only place `alembic upgrade head` runs in a container deploy.
set -euo pipefail

# On managed Postgres providers (e.g. Render) there's no docker-entrypoint-initdb.d hook to
# create the restricted mainai_app role the way backend/db-init/01-app-role.sh does for local
# Docker Compose — this does the equivalent, idempotently, via the admin connection
# (DATABASE_URL). Only runs when MAINAI_APP_PASSWORD is set; local Docker Compose never sets
# it on the backend container (APP_DATABASE_URL is already set there directly), so this is a
# no-op locally. See scripts/ensure_app_role.py and docs/RENDER_DEPLOY.md.
if [ -n "${MAINAI_APP_PASSWORD:-}" ]; then
  echo "Skapar/uppdaterar mainai_app-rollen..."
  export RENDER_ENV_FILE
  RENDER_ENV_FILE="$(mktemp)"
  python scripts/ensure_app_role.py
  # shellcheck disable=SC1090
  source "$RENDER_ENV_FILE"
  rm -f "$RENDER_ENV_FILE"
fi

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Kör alembic upgrade head..."
  alembic upgrade head
  # S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8): re-applies and verifies the
  # narrowed mainai_app privilege state on the memory-provenance tables/functions on EVERY
  # boot, not just the first one — ensure_app_role.py above re-grants ALL PRIVILEGES to
  # mainai_app unconditionally on every boot (not just role creation), which would silently
  # undo a REVOKE that only ran once, at migration time. Runs in the same conditional branch
  # as the migration itself (not unconditionally): the effect is database-global, so only
  # the one container that just ran the migration needs to also apply it — the durable-worker
  # container (RUN_MIGRATIONS=false) shares the same reasoning that already governs it
  # skipping `alembic upgrade head` here.
  echo "Kör apply_runtime_privileges..."
  python scripts/apply_runtime_privileges.py
else
  # The durable-worker package's worker service (docker-compose.vps.yml) runs from this
  # exact image with a different `command:`, not a different image — without this, worker
  # and backend containers starting at the same time would both run `alembic upgrade head`
  # concurrently against the same database. Alembic's own advisory lock makes that safe
  # rather than corrupting, but it's still pure redundant work and unnecessary lock
  # contention on every deploy, so the worker sets RUN_MIGRATIONS=false and lets backend be
  # the one place migrations actually run.
  echo "RUN_MIGRATIONS=false, hoppar över alembic upgrade head."
fi

exec "$@"
