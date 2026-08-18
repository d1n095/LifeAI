#!/usr/bin/env bash
# Run backend pytest against Cloud Agent Postgres (:5432 + password auth), not conftest's
# default localhost:5433 peer-auth assumption. Sources backend/.env, derives disposable
# test DATABASE_URL / APP_DATABASE_URL on the same host/credentials, then exec pytest.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "backend/.env is missing — run .cursor/install.sh first" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
: "${DATABASE_URL:?DATABASE_URL must be set in backend/.env}"
: "${MAINAI_APP_PASSWORD:?MAINAI_APP_PASSWORD must be set in backend/.env}"

eval "$(python3 "$REPO/.cursor/derive_pytest_env.py")"

cd "$REPO/backend"
# shellcheck disable=SC1091
. .venv/bin/activate
exec pytest "$@"
