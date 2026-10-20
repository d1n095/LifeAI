#!/bin/bash
# Live local stack for UI / proxy checks: real backend (Dockerfile flags) + real `next start` proxy + optional worker.
# Usage: live_stack.sh up <candidate-worktree> <built-frontend-dir> | live_stack.sh down
# Loopback only; disposable DB; synthetic founder password; providers faked like CI E2E (scripts/ci/_e2e_fakes.py).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; BP=18073; NP=13073; STATE="$HERE/.live_stack"
if [ "$1" = down ]; then
  pkill -f "next start -p $NP"; pkill -f "run_live_backend.py"; sleep 1
  [ -f "$STATE" ] && psql -h localhost -p 5433 -U lifeos -d postgres -qc "DROP DATABASE IF EXISTS $(cat "$STATE") WITH (FORCE)"; rm -f "$STATE"; exit 0
fi
TARGET="$(cd "$2" && pwd)"; FE="$(cd "$3" && pwd)"; VENV="$HERE/../../../venv312/bin"; DB="examiner_live_$$"; echo "$DB" > "$STATE"
export NEXT_TELEMETRY_DISABLED=1 DATABASE_URL="postgresql://lifeos@localhost:5433/$DB" APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@localhost:5433/$DB" \
  REDIS_URL="redis://127.0.0.1:6390/6" SECRET_KEY=examiner-live FOUNDER_EMAIL=founder@lifeos.local FOUNDER_PASSWORD='TestFounderPassword123!' \
  FRONTEND_ORIGINS="http://127.0.0.1:$NP" PUBLIC_APP_URL="http://127.0.0.1:$NP" ENABLE_SCHEDULED_CLEANUP=false OPENAI_API_KEY=fake-key-for-e2e \
  STORAGE_ROOT="$(mktemp -d)" HOME="$(mktemp -d)" E2E_EMAIL_LOG_PATH="$HERE/.live_emails.jsonl"
unset GH_TOKEN GITHUB_TOKEN ANTHROPIC_API_KEY
redis-cli -p 6390 -n 6 flushdb >/dev/null
psql -h localhost -p 5433 -U lifeos -d postgres -qc "CREATE DATABASE $DB" || exit 2
psql -h localhost -p 5433 -U lifeos -d $DB -qc "GRANT USAGE ON SCHEMA public TO mainai_app; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mainai_app; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO mainai_app;"
( cd "$TARGET/backend" && "$VENV/alembic" upgrade head > "$HERE/live_alembic.log" 2>&1 )
# Same fakes as CI's run_e2e_backend.py, but with the Dockerfile's proxy-header flags.
cat > "$HERE/run_live_backend.py" <<PY
import os, sys
sys.path.insert(0, "$TARGET/backend"); sys.path.insert(0, "$TARGET/backend/scripts/ci")
from _e2e_fakes import install_email_fake, install_provider_fakes
install_provider_fakes(); install_email_fake(os.environ["E2E_EMAIL_LOG_PATH"], truncate=True)
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port=$BP, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
PY
( cd "$TARGET/backend" && nohup "$VENV/python" "$HERE/run_live_backend.py" > "$HERE/live_backend.log" 2>&1 & )
( cd "$FE" && INTERNAL_API_URL="http://127.0.0.1:$BP" nohup npx next start -p $NP -H 127.0.0.1 > "$HERE/live_next.log" 2>&1 & )
for i in $(seq 1 80); do curl -sf -o /dev/null "http://127.0.0.1:$NP/api/health" && { echo "UP frontend=http://127.0.0.1:$NP backend=127.0.0.1:$BP db=$DB"; exit 0; }; sleep 0.5; done
echo "stack did not come up"; tail -20 "$HERE/live_backend.log"; exit 1
