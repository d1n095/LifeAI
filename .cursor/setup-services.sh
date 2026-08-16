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

echo "--> Ensuring Postgres 16 cluster is online"
if ! pg_lsclusters -h 2>/dev/null | awk '$1=="16" && $2=="main" {print $4}' | grep -q online; then
  sudo pg_ctlcluster 16 main start
fi

echo "--> Ensuring Redis is running"
if ! redis-cli ping >/dev/null 2>&1; then
  sudo redis-server /etc/redis/redis.conf --daemonize yes
fi

echo "--> Waiting for Postgres to accept connections"
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "--> Provisioning roles (lifeos superuser + restricted mainai_app runtime role)"
# Mirrors backend/db-init/01-app-role.sh: `lifeos` owns the schema and runs migrations; the
# non-superuser `mainai_app` role is what the app queries through so Row-Level Security is
# actually enforced (a superuser bypasses RLS). Passwords match backend/.env's connection URLs.
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='lifeos') THEN
    CREATE ROLE lifeos LOGIN SUPERUSER PASSWORD 'lifeos';
  ELSE
    ALTER ROLE lifeos LOGIN SUPERUSER PASSWORD 'lifeos';
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mainai_app') THEN
    CREATE ROLE mainai_app LOGIN PASSWORD 'mainai_app';
  ELSE
    ALTER ROLE mainai_app LOGIN PASSWORD 'mainai_app';
  END IF;
END $$;
SQL

echo "--> Ensuring the lifeos database exists"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='lifeos'" | grep -q 1; then
  sudo -u postgres createdb -O lifeos lifeos
fi

echo "--> Granting the restricted runtime role (SELECT/INSERT/UPDATE/DELETE only — never TRUNCATE)"
sudo -u postgres psql -v ON_ERROR_STOP=1 -d lifeos <<'SQL'
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
set -a
# shellcheck disable=SC1091
. ./.env
set +a
alembic upgrade head
python scripts/security/apply_runtime_privileges.py

echo "--> Database ready."
