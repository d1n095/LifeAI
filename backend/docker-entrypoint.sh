#!/bin/bash
# Applies pending Alembic migrations, then starts the app. Migrations run through
# DATABASE_URL (the superuser/migration role — see app/db.py and app/config.py); the app
# itself serves traffic through the separate, restricted APP_DATABASE_URL role. Schema is
# never created or altered by the app at request-serving startup (see app/main.py) — this
# is the one and only place `alembic upgrade head` runs in a container deploy.
set -euo pipefail

# RUN_PRIVILEGE_BOOT (default true): gates BOTH the role-provisioning/privilege-widening step
# below AND apply_runtime_privileges.py's mutating narrow-down step. Founder-reported
# production incident (VPS deploy): before this flag existed, ensure_app_role.py's mutating
# GRANT ALL + S1A re-narrow AND apply_runtime_privileges.py's own REVOKE/GRANT ran
# unconditionally on EVERY container sharing this image — including the durable-worker
# container, which ran them concurrently with the backend container's own identical
# statements against the same catalog rows (pg_class.relacl/pg_proc.proacl), occasionally
# hitting Postgres's "tuple concurrently updated" and crash-looping the worker. Exactly ONE
# container (the backend) should ever mutate mainai_app's privileges; docker-compose.vps.yml
# sets RUN_PRIVILEGE_BOOT=false on the worker service. When false, the two steps below run
# their read-only equivalents instead (--derive-only / --verify-only) — never zero-effect: the
# worker still needs APP_DATABASE_URL and still fails closed if the privilege state it reads is
# wrong, it just never itself writes to it. See backend/scripts/s1a_privilege_policy.py's
# `acquire_privilege_boot_lock()` for the remaining defense-in-depth this flag doesn't cover
# (two BACKEND replicas racing each other).
RUN_PRIVILEGE_BOOT="${RUN_PRIVILEGE_BOOT:-true}"

# On managed Postgres providers (e.g. Render) there's no docker-entrypoint-initdb.d hook to
# create the restricted mainai_app role the way backend/db-init/01-app-role.sh does for local
# Docker Compose — this does the equivalent, idempotently, via the admin connection
# (DATABASE_URL). Only runs when MAINAI_APP_PASSWORD is set; local Docker Compose never sets
# it on the backend container (APP_DATABASE_URL is already set there directly), so this is a
# no-op locally. See scripts/ensure_app_role.py and docs/RENDER_DEPLOY.md.
if [ -n "${MAINAI_APP_PASSWORD:-}" ]; then
  export RENDER_ENV_FILE
  RENDER_ENV_FILE="$(mktemp)"
  if [ "$RUN_PRIVILEGE_BOOT" = "true" ]; then
    echo "Skapar/uppdaterar mainai_app-rollen..."
    python scripts/ensure_app_role.py
  else
    echo "RUN_PRIVILEGE_BOOT=false, härleder APP_DATABASE_URL utan att mutera rollen..."
    python scripts/ensure_app_role.py --derive-only
  fi
  # shellcheck disable=SC1090
  source "$RENDER_ENV_FILE"
  rm -f "$RENDER_ENV_FILE"
fi

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  echo "Kör alembic upgrade head..."
  alembic upgrade head
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

# S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8): re-applies and verifies the narrowed
# mainai_app privilege state on the memory-provenance tables/functions. Only the backend
# (RUN_PRIVILEGE_BOOT=true) actually mutates; the worker verifies the SAME policy read-only
# instead (see apply_runtime_privileges.py's `--verify-only` for the bounded retry this needs,
# since the worker's read can legitimately run concurrently with the backend's own first-time
# narrowing on a schema-upgrading deploy) — it still fails closed (non-zero exit, container
# never reaches `exec "$@"` below) if the privilege state it reads is wrong, exactly like the
# backend does for its own mutating path.
if [ "$RUN_PRIVILEGE_BOOT" = "true" ]; then
  echo "Kör apply_runtime_privileges..."
  python scripts/apply_runtime_privileges.py
else
  echo "RUN_PRIVILEGE_BOOT=false, verifierar privilege-state read-only (apply_runtime_privileges.py --verify-only)..."
  python scripts/apply_runtime_privileges.py --verify-only
fi

exec "$@"
