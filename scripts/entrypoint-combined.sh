#!/bin/bash
# Single-container Render Free entrypoint: runs FastAPI (loopback-only) and Next.js
# (publicly bound to $PORT) as sibling processes, supervised here. See
# docs/RENDER_DEPLOY.md for why this exists (Render Free doesn't offer a Private Service
# plan, so FastAPI's isolation comes from binding to 127.0.0.1 instead — nothing outside
# this container can reach it regardless, since only $PORT is ever published).
#
# Deliberately NOT `set -e` at the top level. This script's whole job past startup is
# `wait -n` on two long-running background jobs — wait -n returns the *exit status of
# whichever job finished*, which is routinely nonzero (e.g. 143 for a process that got
# SIGTERM'd), not an error in this script. Under `set -e`, that nonzero return would abort
# the script on that exact line, before EXIT_CODE is captured and before the surviving
# sibling process is torn down — an orphaned process and skipped cleanup. `set -u` and
# `pipefail` stay on; only bare errexit is the problem here, so it stays off, and the one
# place a command's failure needs handling (`wait -n`) does so explicitly via `if`.
set -uo pipefail

BACKEND_PID=""
FRONTEND_PID=""
CLEANED_UP=0

cleanup() {
  # Idempotent — the EXIT trap and an explicit signal handler can both end up calling this
  # for the same shutdown; only the first call should actually do anything.
  if [ "$CLEANED_UP" -eq 1 ]; then
    return
  fi
  CLEANED_UP=1
  echo "[entrypoint] shutting down..."
  if [ -n "$FRONTEND_PID" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill -TERM "$FRONTEND_PID" 2>/dev/null || true
  fi
  if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill -TERM "$BACKEND_PID" 2>/dev/null || true
  fi
  [ -n "$FRONTEND_PID" ] && wait "$FRONTEND_PID" 2>/dev/null
  [ -n "$BACKEND_PID" ] && wait "$BACKEND_PID" 2>/dev/null
  echo "[entrypoint] shutdown complete."
}

# EXIT fires no matter how this script ends — normal exit, a signal, or an unexpected
# error earlier in the script — not just the two signals we explicitly expect below. That's
# the "cleanup even on unexpected exit" guarantee: cleanup is reachable from every path,
# not bolted onto just the two cases we thought to handle.
trap cleanup EXIT
trap 'exit 143' TERM   # 128+15 — conventional exit code for SIGTERM (Docker/Render's stop signal)
trap 'exit 130' INT    # 128+2  — conventional exit code for SIGINT (Ctrl-C, interactive use)

# E2E_MOCK_MODE (unset/false in every real deployment — set only by
# .github/workflows/ci.yml's combined-container-verify job) swaps the real uvicorn command for
# backend/scripts/ci/run_e2e_backend.py, which fakes only the outbound AI-provider and email
# calls (see that file) so registration/verification/chat E2E runs are deterministic and need
# neither real OpenAI network access nor a real SMTP server. Everything else — role
# provisioning, Alembic migrations, auth, cookies, CSRF, RLS, rate limiting — is still the
# genuine application code, unchanged by this flag.
if [ "${E2E_MOCK_MODE:-false}" = "true" ]; then
  BACKEND_CMD=(python scripts/ci/run_e2e_backend.py)
else
  BACKEND_CMD=(uvicorn app.main:app --host 127.0.0.1 --port 8000 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1)
fi

# Reuses the existing role-provisioning + alembic-migration entrypoint unchanged — not
# duplicated here. See backend/docker-entrypoint.sh.
#
# TEST_DELAY_BACKEND_STARTUP_SECONDS is a test-only hook (never set in a real deployment —
# only .github/workflows/ci.yml's combined-container-verify job sets it) that reproduces a
# slow backend startup on demand: sleeping here, before docker-entrypoint.sh even runs,
# means the backend port genuinely isn't listening yet for that long, the same as if role
# provisioning/migrations/uvicorn boot were just slow. It exists to make the fix below
# provable against the real container, not just asserted.
(cd /app/backend && \
  if [ -n "${TEST_DELAY_BACKEND_STARTUP_SECONDS:-}" ]; then \
    echo "[entrypoint] TEST HOOK: delaying backend startup by ${TEST_DELAY_BACKEND_STARTUP_SECONDS}s (TEST_DELAY_BACKEND_STARTUP_SECONDS is set)"; \
    sleep "${TEST_DELAY_BACKEND_STARTUP_SECONDS}"; \
  fi; \
  E2E_BACKEND_HOST=127.0.0.1 E2E_BACKEND_PORT=8000 ./docker-entrypoint.sh "${BACKEND_CMD[@]}") &
BACKEND_PID=$!

echo "[entrypoint] backend pid=$BACKEND_PID (127.0.0.1:8000, loopback-only) — waiting for it to become healthy before starting the frontend..."

# Verified production bug: without this wait, Next's proxy route (app/(shell)'s /api/*
# handler, see frontend/lib/api.ts) starts accepting public traffic and immediately tries
# 127.0.0.1:8000 before FastAPI has finished role provisioning + `alembic upgrade head` +
# uvicorn boot — every request in that window gets ECONNREFUSED, surfaced to the browser as
# a 502. Gating frontend startup on a real /api/health 200 (not just "the process exists")
# closes that window entirely instead of relying on timing that happens to work locally.
#
# The ?probe=internal-startup-gate query string changes nothing about how app/routers/health.py
# answers (FastAPI ignores unrecognized query params) — it exists purely so this loopback-only
# call is textually distinguishable from Render's own public-edge health probe in
# backend/uvicorn's access log, when reading logs after the fact (see docs/RENDER_DEPLOY.md's
# 2026-07-20 502-despite-Live entry). Render's probe never carries this param — it can't, since
# it never sees this script's source.
BACKEND_HEALTH_URL="http://127.0.0.1:8000/api/health?probe=internal-startup-gate"
BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS="${BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS:-90}"
backend_ready=0
elapsed=0
while [ "$elapsed" -lt "$BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS" ]; do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "[entrypoint] backend process (pid=$BACKEND_PID) exited before becoming healthy."
    break
  fi
  if curl -sf -o /dev/null "$BACKEND_HEALTH_URL"; then
    backend_ready=1
    break
  fi
  sleep 1
  elapsed=$((elapsed + 1))
done

if [ "$backend_ready" -ne 1 ]; then
  echo "[entrypoint] backend did not become healthy at $BACKEND_HEALTH_URL within ${BACKEND_HEALTH_CHECK_TIMEOUT_SECONDS}s — aborting startup, never starting the frontend."
  exit 1
fi
echo "[entrypoint] backend is healthy after ${elapsed}s — starting frontend."

# Next's standalone server.js resolves .next/ relative to its OWN working directory, not
# relative to the script's location — found the hard way, running this without the `cd`
# fails with "Could not find a production build in the './.next' directory" even though the
# build is right there, just not relative to wherever this script happened to be invoked
# from.
(cd /app/frontend-standalone && PORT="${PORT:-3000}" node server.js) &
FRONTEND_PID=$!

echo "[entrypoint] frontend pid=$FRONTEND_PID (0.0.0.0:${PORT:-3000})"

# See the top-of-file note on why this isn't just `wait -n "$BACKEND_PID" "$FRONTEND_PID"`
# under `set -e`. Treating its result as data (via `if`), not a script error, is the fix.
if wait -n "$BACKEND_PID" "$FRONTEND_PID"; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi

echo "[entrypoint] a process exited (code $EXIT_CODE) — tearing down the other."
exit "$EXIT_CODE"
