#!/usr/bin/env bash
# Starts Postgres + Redis (no systemd in the Cloud Agent VM) and idempotently provisions the
# database roles, the `lifeos` database, the pgvector extension (via migrations), the schema
# (alembic upgrade head) and the narrowed runtime privilege policy. Called by both
# .cursor/install.sh (durable first-time setup) and .cursor/start.sh (per-boot reconciliation).
#
# Idempotent and restart-tolerant: services are only started if not already running, and every
# SQL statement guards against already-applied state.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "backend/.env is missing — run .cursor/install.sh first" >&2
  exit 1
fi

# Role passwords MUST come from the same .env the app will load. Hardcoding `mainai_app`
# here while APP_DATABASE_URL / MAINAI_APP_PASSWORD can differ (or be edited later) produced
# a silent auth mismatch: migrations ran as lifeos, then the API/worker failed as mainai_app.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${MAINAI_APP_PASSWORD:?MAINAI_APP_PASSWORD must be set in backend/.env}"
: "${DATABASE_URL:?DATABASE_URL must be set in backend/.env}"

# Derive the lifeos superuser password and database name from DATABASE_URL (same
# credentials Alembic will use). Never echo DATABASE_URL or the password.
eval "$(python3 "$REPO/.cursor/parse_database_url.py")"
: "${LIFEOS_PASSWORD:?failed to parse lifeos password from DATABASE_URL}"
: "${LIFEOS_DB:?failed to parse database name from DATABASE_URL}"

echo "--> Syncing APP_DATABASE_URL with MAINAI_APP_PASSWORD (same derivation as production worker)"
python3 "$REPO/.cursor/sync_app_database_url.py" "$ENV_FILE"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${APP_DATABASE_URL:?APP_DATABASE_URL must be set after sync — check DATABASE_URL and MAINAI_APP_PASSWORD}"

echo "--> Ensuring Postgres 16 cluster is online"
if ! pg_lsclusters -h 2>/dev/null | awk '$1=="16" && $2=="main" {print $4}' | grep -q online; then
  sudo pg_ctlcluster 16 main start
fi

echo "--> Ensuring Redis is running"
if ! redis-cli ping >/dev/null 2>&1; then
  sudo redis-server /etc/redis/redis.conf --daemonize yes
fi

echo "--> Waiting for Postgres to accept connections"
ready=0
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then
    ready=1
    break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "Postgres did not become ready" >&2
  exit 1
fi

echo "--> Waiting for Redis to accept connections"
redis_ready=0
for _ in $(seq 1 30); do
  if redis-cli ping >/dev/null 2>&1; then
    redis_ready=1
    break
  fi
  sleep 1
done
if [ "$redis_ready" -ne 1 ]; then
  echo "Redis did not become ready" >&2
  exit 1
fi

echo "--> Provisioning roles (lifeos superuser + restricted mainai_app runtime role)"
# Mirrors backend/db-init/01-app-role.sh: `lifeos` owns the schema and runs migrations; the
# non-superuser `mainai_app` role is what the app queries through so Row-Level Security is
# actually enforced (a superuser bypasses RLS). Passwords are psql variables
# (:'lifeos_pw' from DATABASE_URL, :'app_pw' from MAINAI_APP_PASSWORD) — never interpolated
# into SQL text and never logged.
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -v lifeos_pw="$LIFEOS_PASSWORD" \
  -v app_pw="$MAINAI_APP_PASSWORD" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='lifeos') THEN
    CREATE ROLE lifeos LOGIN SUPERUSER PASSWORD :'lifeos_pw';
  ELSE
    ALTER ROLE lifeos LOGIN SUPERUSER PASSWORD :'lifeos_pw';
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mainai_app') THEN
    CREATE ROLE mainai_app LOGIN PASSWORD :'app_pw';
  ELSE
    ALTER ROLE mainai_app LOGIN PASSWORD :'app_pw';
  END IF;
END $$;
SQL

echo "--> Ensuring the configured database exists"
if ! sudo -u postgres psql -v ON_ERROR_STOP=1 -v db_name="$LIFEOS_DB" \
  -tAc "SELECT 1 FROM pg_database WHERE datname = :'db_name'" | grep -q 1; then
  sudo -u postgres createdb -O lifeos "$LIFEOS_DB"
fi

echo "--> Granting the restricted runtime role (SELECT/INSERT/UPDATE/DELETE only — never TRUNCATE)"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$LIFEOS_DB" <<'SQL'
GRANT USAGE ON SCHEMA public TO mainai_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mainai_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lifeos IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mainai_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lifeos IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;
SQL

echo "--> Applying migrations (alembic upgrade head) + runtime privilege policy"
cd "$REPO/backend"
# shellcheck disable=SC1091
. .venv/bin/activate
alembic upgrade head
python scripts/security/apply_runtime_privileges.py

echo "--> Database ready."
