#!/bin/bash
# Applies pending Alembic migrations, then starts the app. Migrations run through
# DATABASE_URL (the superuser/migration role — see app/db.py and app/config.py); the app
# itself serves traffic through the separate, restricted APP_DATABASE_URL role. Schema is
# never created or altered by the app at request-serving startup (see app/main.py) — this
# is the one and only place `alembic upgrade head` runs in a container deploy.
set -euo pipefail

echo "Kör alembic upgrade head..."
alembic upgrade head

exec "$@"
