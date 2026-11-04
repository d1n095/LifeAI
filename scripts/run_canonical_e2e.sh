#!/usr/bin/env bash
# Canonical browser-E2E runner with the REAL prerequisites each spec group needs.
#
# The Playwright specs under frontend/e2e all authenticate as the single founder account and
# run serially with no per-test cleanup, so a naive `npx playwright test` over the whole
# directory is order-sensitive and cross-contaminates (e.g. the library-import specs leave the
# founder's Library non-empty, which then breaks shell-pages' empty-state assertion and the
# refresh-token-rotation security spec). CI never hits this because it runs a curated, mutually
# compatible subset per job, each against its OWN freshly-created database. This script does the
# same for the FULL suite: it recreates a fresh e2e database for each group via the documented
# bootstrap (ensure_app_role.py -> alembic upgrade head -> apply_runtime_privileges.py) and
# starts exactly the processes that group needs:
#
#   A. functional      — auth, security, account, shell-pages, mainai-jobs-pagination
#                        (generous auth rate limits; web app only).
#   B. library/studio  — founder-knowledge-studio(+media), library-upload-queue,
#                        library-workbench-mobile (generous limits; web app AND the durable
#                        worker, both with the shared AI/email fakes — imports are processed by
#                        the worker, see backend/scripts/ci/run_e2e_worker.py).
#   C. same-origin     — same-origin-proxy under playwright.proxy.config.ts (the standalone
#                        production server on :3021).
#   D. rate-limit      — rate-limit against the REAL default limits on a clean backend.
#
# Requires Postgres + Redis already running and backend/.env present (run .cursor/install.sh
# first). Never used in production; test infrastructure only.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO/backend"
FRONTEND="$REPO/frontend"

# --- Config -----------------------------------------------------------------------------
E2E_DB="${E2E_DB:-lifeos_e2e}"
LIFEOS_SUPER_URL="postgresql://lifeos:lifeos@localhost:5432/${E2E_DB}"
APP_URL="postgresql://mainai_app:mainai_app@localhost:5432/${E2E_DB}"
export E2E_STORAGE_ROOT="${E2E_STORAGE_ROOT:-$HOME/.lifeai-e2e/uploads}"
BACKEND_PORT=8010
EMAIL_LOG="$BACKEND/e2e_test_emails.jsonl"

# Env shared by every backend/worker/playwright invocation below.
common_env() {
  export DATABASE_URL="$LIFEOS_SUPER_URL"
  export APP_DATABASE_URL="$APP_URL"
  export MAINAI_APP_PASSWORD="mainai_app"
  export SECRET_KEY="ci-e2e-secret-key"
  export FOUNDER_EMAIL="founder@lifeos.local"
  export FOUNDER_PASSWORD="TestFounderPassword123!"
  export FRONTEND_ORIGINS="http://127.0.0.1:3020,http://localhost:3020,http://127.0.0.1:3021"
  export PUBLIC_APP_URL="http://127.0.0.1:3020"
  export REDIS_URL="redis://localhost:6379/0"
  export ENABLE_SCHEDULED_CLEANUP="false"
  export OPENAI_API_KEY="fake-key-for-e2e"
  export STORAGE_ROOT="$E2E_STORAGE_ROOT"
  export E2E_EMAIL_LOG_PATH="$EMAIL_LOG"
  export E2E_FOUNDER_PASSWORD="TestFounderPassword123!"
  export E2E_DATABASE_URL="$LIFEOS_SUPER_URL"
}

fresh_db() {
  echo "  -> recreating fresh $E2E_DB"
  rm -rf "$E2E_STORAGE_ROOT"; mkdir -p "$E2E_STORAGE_ROOT"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -q >/dev/null <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${E2E_DB}' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ${E2E_DB};
CREATE DATABASE ${E2E_DB} OWNER lifeos;
SQL
  ( cd "$BACKEND" && . .venv/bin/activate \
      && python scripts/security/ensure_app_role.py >/dev/null \
      && alembic upgrade head >/dev/null 2>&1 \
      && python scripts/security/apply_runtime_privileges.py >/dev/null )
}

BACKEND_PID=""; WORKER_PID=""
start_backend() {  # $1=extra env (generous|default)
  ( cd "$BACKEND" && . .venv/bin/activate && common_env
    if [ "${1:-}" = "generous" ]; then
      export RATE_LIMIT_LOGIN_PER_MINUTE=1000 RATE_LIMIT_REGISTER_PER_MINUTE=1000 \
             RATE_LIMIT_FORGOT_PASSWORD_PER_MINUTE=1000 RATE_LIMIT_RESET_PASSWORD_PER_MINUTE=1000 \
             RATE_LIMIT_VERIFY_EMAIL_PER_MINUTE=1000
    fi
    python -c "import redis; redis.from_url('redis://localhost:6379/0').flushdb()" 2>/dev/null || true
    nohup python scripts/ci/run_e2e_backend.py >/tmp/canonical_e2e_backend.log 2>&1 &
    echo $! > /tmp/canonical_e2e_backend.pid )
  BACKEND_PID="$(cat /tmp/canonical_e2e_backend.pid)"
  for _ in $(seq 1 40); do curl -sf -o /dev/null "http://127.0.0.1:${BACKEND_PORT}/api/health" && break; sleep 1; done
}
start_worker() {
  ( cd "$BACKEND" && . .venv/bin/activate && common_env
    nohup python scripts/ci/run_e2e_worker.py >/tmp/canonical_e2e_worker.log 2>&1 &
    echo $! > /tmp/canonical_e2e_worker.pid )
  WORKER_PID="$(cat /tmp/canonical_e2e_worker.pid)"
}
stop_procs() {
  [ -n "$WORKER_PID" ] && kill "$WORKER_PID" 2>/dev/null || true
  [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null || true
  WORKER_PID=""; BACKEND_PID=""
  sleep 1
}
trap stop_procs EXIT

RESULTS=()
run_group() {  # $1=label ; rest handled by caller via GROUP_CMD
  local label="$1"; shift
  echo "== Group ${label} =="
  if "$@"; then RESULTS+=("PASS  ${label}"); else RESULTS+=("FAIL  ${label}"); fi
  stop_procs
}

common_env
echo "==> Building frontend once (standalone) for the proxy group"
( cd "$FRONTEND" && NEXT_PUBLIC_API_URL= npx next build >/tmp/canonical_e2e_build.log 2>&1 \
    && cp -r public .next/standalone/public 2>/dev/null; cp -r .next/static .next/standalone/.next/static 2>/dev/null ) || true

group_functional() { fresh_db; start_backend generous; ( cd "$FRONTEND" && common_env && npx playwright test --project=chromium \
    e2e/auth.spec.ts e2e/security.spec.ts e2e/account.spec.ts e2e/shell-pages.spec.ts e2e/mainai-jobs-pagination.spec.ts ); }
group_library() { fresh_db; start_backend generous; start_worker; ( cd "$FRONTEND" && common_env && npx playwright test --project=chromium \
    e2e/founder-knowledge-studio.spec.ts e2e/founder-knowledge-studio-media.spec.ts \
    e2e/library-upload-queue.spec.ts e2e/library-workbench-mobile.spec.ts ); }
group_proxy() { fresh_db; start_backend generous; ( cd "$FRONTEND" && common_env && npx playwright test --config=playwright.proxy.config.ts --project=chromium ); }
group_ratelimit() { fresh_db; start_backend default; ( cd "$FRONTEND" && common_env && npx playwright test --project=chromium e2e/rate-limit.spec.ts ); }

run_group "A functional"    group_functional
run_group "B library/studio" group_library
run_group "C same-origin"   group_proxy
run_group "D rate-limit"    group_ratelimit

echo
echo "==================== CANONICAL E2E SUMMARY ===================="
printf '%s\n' "${RESULTS[@]}"
if printf '%s\n' "${RESULTS[@]}" | grep -q '^FAIL'; then echo "RESULT: FAIL"; exit 1; fi
echo "RESULT: PASS"
