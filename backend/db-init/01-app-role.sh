#!/bin/bash
# Runs once, automatically, the first time the Postgres data volume is initialized
# (docker-entrypoint-initdb.d convention). Creates a non-superuser role for the backend
# to use at runtime, so Row-Level Security policies are actually enforced — the
# POSTGRES_USER role is a superuser and always bypasses RLS, so the app must never
# query through it (see backend/app/db.py).
set -euo pipefail

: "${MAINAI_APP_PASSWORD:?MAINAI_APP_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mainai_app') THEN
        CREATE ROLE mainai_app LOGIN PASSWORD '${MAINAI_APP_PASSWORD}';
      ELSE
        ALTER ROLE mainai_app LOGIN PASSWORD '${MAINAI_APP_PASSWORD}';
      END IF;
    END
    \$\$;

    GRANT USAGE ON SCHEMA public TO mainai_app;
    -- SELECT, INSERT, UPDATE, DELETE -- deliberately NOT "ALL PRIVILEGES", which would also
    -- mean TRUNCATE, REFERENCES and TRIGGER. No runtime code path uses any of those three,
    -- and TRUNCATE is not subject to Row-Level Security at all (it is a whole-table
    -- operation, so no RLS policy is ever consulted) -- which would have let the runtime
    -- role wipe every owner's rows out of an RLS-protected table in one statement. See
    -- _NEVER_GRANTED_TABLE_PRIVS in backend/scripts/s1a_privilege_policy.py, which enforces
    -- and re-verifies the same floor on every boot for databases provisioned before this.
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO mainai_app;

    -- Tables created later by \$POSTGRES_USER (the backend runs its schema migration
    -- through that role) automatically grant the same four privileges to mainai_app too.
    ALTER DEFAULT PRIVILEGES FOR ROLE "$POSTGRES_USER" IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mainai_app;
    ALTER DEFAULT PRIVILEGES FOR ROLE "$POSTGRES_USER" IN SCHEMA public
      GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;
EOSQL
