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

echo "--> Provisioning the lifeos admin/migration superuser (dev-only admin bootstrap)"
# The lifeos role is the schema owner / migration role — the local-dev equivalent of
# docker-compose's POSTGRES_USER and a managed provider's project owner. It is deliberately a
# SUPERUSER because migrations run CREATE EXTENSION and enable Row-Level Security through it;
# it is NEVER the role the app serves requests through (that is mainai_app, below, which is a
# plain non-superuser role). psql only interpolates :'var' in top-level statements, NEVER
# inside a DO $$...$$ block — the previous version put :'lifeos_pw' inside a DO block, which
# psql sent literally and Postgres rejected with a syntax error, so a clean install could not
# self-provision at all. The role is now created with a plain \gexec guard and its password is
# set by a top-level ALTER ROLE, where :'lifeos_pw' is interpolated correctly. Never logged.
sudo -u postgres psql -v ON_ERROR_STOP=1 -v lifeos_pw="$LIFEOS_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE lifeos LOGIN SUPERUSER'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lifeos')
\gexec
ALTER ROLE lifeos WITH LOGIN SUPERUSER PASSWORD :'lifeos_pw';
SQL

echo "--> Ensuring the configured database exists"
if ! sudo -u postgres psql -v ON_ERROR_STOP=1 -v db_name="$LIFEOS_DB" \
  -tAc "SELECT 1 FROM pg_database WHERE datname = :'db_name'" | grep -q 1; then
  sudo -u postgres createdb -O lifeos "$LIFEOS_DB"
fi

echo "--> Provisioning the restricted mainai_app runtime role via the documented bootstrap (ensure_app_role.py)"
# Use the SAME documented production/VPS bootstrap the container entrypoint uses
# (backend/scripts/security/ensure_app_role.py) instead of ad-hoc GRANT SQL here. It creates
# mainai_app with least privilege (SELECT/INSERT/UPDATE/DELETE only — never TRUNCATE/REFERENCES/
# TRIGGER, and NEVER superuser), sets matching default privileges so tables created by the later
# migration are granted automatically, and re-narrows the S1A objects. This is what makes a
# clean install need NO manual SQL and guarantees the Cloud Agent dev path cannot silently
# diverge from production. Runs against the lifeos admin DATABASE_URL, before migrations, in the
# exact order backend/docker-entrypoint.sh uses (ensure_app_role -> upgrade head ->
# apply_runtime_privileges). DATABASE_URL + MAINAI_APP_PASSWORD are already exported from .env.
cd "$REPO/backend"
# shellcheck disable=SC1091
. .venv/bin/activate
python scripts/security/ensure_app_role.py

echo "--> Applying migrations (alembic upgrade head) + runtime privilege policy"
alembic upgrade head
python scripts/security/apply_runtime_privileges.py

echo "--> Database ready."
