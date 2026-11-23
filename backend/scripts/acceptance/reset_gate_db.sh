#!/usr/bin/env bash
# Rebuilds the local R15 gate database from scratch: this repo's migrations, the restricted
# runtime role's grants, and scripts/security/apply_runtime_privileges.py's policy check.
#
# This exists because the gate database is throwaway evidence, not state: every acceptance run
# must start from an empty, fully migrated database, and the grants step is easy to forget (the
# API refuses to start without it). It never touches any database other than $GATE_DB.
#
# Usage:
#   set -a; . /tmp/lifeai-r15.env; set +a
#   scripts/acceptance/reset_gate_db.sh
set -euo pipefail

GATE_DB="${GATE_DB:-lifeos_r15_gate}"
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-lifeos}"
BACKEND_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

echo "--> dropping and recreating $GATE_DB on $PGHOST:$PGPORT"
dropdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" --if-exists "$GATE_DB"
createdb -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -O "$PGUSER" "$GATE_DB"
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$GATE_DB" -q -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "--> applying migrations"
cd "$BACKEND_DIR"
DATABASE_URL="postgresql://$PGUSER@$PGHOST:$PGPORT/$GATE_DB" .venv/bin/alembic upgrade head 2>/dev/null \
  || DATABASE_URL="postgresql://$PGUSER@$PGHOST:$PGPORT/$GATE_DB" ../.venv/bin/alembic upgrade head

echo "--> granting the restricted runtime role"
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$GATE_DB" -q -v ON_ERROR_STOP=1 <<'SQL'
GRANT USAGE ON SCHEMA public TO mainai_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mainai_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lifeos IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mainai_app;
ALTER DEFAULT PRIVILEGES FOR ROLE lifeos IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;
REVOKE ALL ON alembic_version FROM mainai_app;
SQL

echo "--> verifying the runtime privilege policy"
DATABASE_URL="postgresql://$PGUSER@$PGHOST:$PGPORT/$GATE_DB" ../.venv/bin/python scripts/security/apply_runtime_privileges.py
echo "--> $GATE_DB ready."
