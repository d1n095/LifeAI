#!/usr/bin/env bash
# Idempotent dependency + database bootstrap for the Life OS / MainAI dev environment.
#
# Runs after the repository is checked out. Installs system packages (Postgres 16 + pgvector,
# Redis, ripgrep), the backend Python venv, the frontend node_modules and the Playwright
# Chromium browser, writes a dev-only backend/.env if one does not already exist, then starts
# Postgres/Redis and provisions + migrates the database (see .cursor/setup-services.sh).
#
# Safe to run repeatedly: every step guards against already being done.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "==> [1/5] System packages (Postgres 16 + pgvector, Redis, ripgrep, build tools)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  postgresql-16 postgresql-16-pgvector postgresql-client-16 \
  redis-server ripgrep \
  python3-venv python3-dev build-essential

echo "==> [2/5] Backend Python venv + dependencies (requirements-dev.txt)"
cd "$REPO/backend"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-dev.txt

echo "==> [3/5] Frontend dependencies + Playwright Chromium"
cd "$REPO/frontend"
npm ci
# Chromium is needed for the Playwright e2e suite (frontend/e2e). --with-deps pulls the
# system libraries the browser needs; harmless/idempotent if already installed.
npx playwright install --with-deps chromium

echo "==> [4/5] Backend .env (dev defaults — generated only if missing)"
ENV_FILE="$REPO/backend/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<EOF
# Auto-generated dev-only environment (see .cursor/install.sh). Never used in production.
ENVIRONMENT=development
SECRET_KEY=dev-only-secret-key-not-for-production-change-me
FOUNDER_EMAIL=founder@lifeos.local
FOUNDER_PASSWORD=TestFounderPassword123!
MAINAI_APP_PASSWORD=mainai_app

DATABASE_URL=postgresql://lifeos:lifeos@localhost:5432/lifeos
REDIS_URL=redis://localhost:6379/0

DEFAULT_LLM_PROVIDER=openai
DEFAULT_LLM_MODEL=gpt-4o-mini
DEFAULT_EMBEDDING_PROVIDER=openai
DEFAULT_EMBEDDING_MODEL=text-embedding-3-small

# Add a real provider key (e.g. OPENAI_API_KEY) here to enable live chat/embeddings.
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_API_KEY=
DEEPSEEK_API_KEY=
OPENROUTER_API_KEY=

CHAT_FALLBACK_ORDER=openai,anthropic,gemini

FRONTEND_ORIGINS=http://localhost:3000
PUBLIC_APP_URL=http://localhost:3000
COOKIE_SECURE=true
COOKIE_SAMESITE=none

ENABLE_SCHEDULED_CLEANUP=true
STORAGE_ROOT=$HOME/.lifeai-dev/uploads
PROJECT_ROOT=$REPO
EOF
  echo "    wrote $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  python3 "$REPO/.cursor/sync_app_database_url.py" "$ENV_FILE"
else
  echo "    $ENV_FILE already exists — left untouched"
fi
mkdir -p "$HOME/.lifeai-dev/uploads"

echo "==> [5/5] Start Postgres/Redis, provision roles/database, run migrations"
bash "$REPO/.cursor/setup-services.sh"

echo "==> install.sh complete."
