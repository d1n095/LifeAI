#!/usr/bin/env bash
# Run backend pytest against Cloud Agent Postgres (:5432 + password auth), not conftest's
# default localhost:5433 peer-auth assumption. Reads only DATABASE_URL + MAINAI_APP_PASSWORD
# from backend/.env (never exports the whole dev .env — that would pin REDIS_URL to db 0 and
# bypass conftest's per-process isolation defaults).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "backend/.env is missing — run .cursor/install.sh first" >&2
  exit 1
fi

eval "$(python3 "$REPO/.cursor/derive_pytest_env.py" "$ENV_FILE")"

cd "$REPO/backend"
# shellcheck disable=SC1091
. .venv/bin/activate
exec pytest "$@"
