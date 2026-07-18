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

# Reuses the existing role-provisioning + alembic-migration entrypoint unchanged — not
# duplicated here. See backend/docker-entrypoint.sh.
(cd /app/backend && ./docker-entrypoint.sh \
  uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1) &
BACKEND_PID=$!

# Next's standalone server.js resolves .next/ relative to its OWN working directory, not
# relative to the script's location — found the hard way, running this without the `cd`
# fails with "Could not find a production build in the './.next' directory" even though the
# build is right there, just not relative to wherever this script happened to be invoked
# from.
(cd /app/frontend-standalone && PORT="${PORT:-3000}" node server.js) &
FRONTEND_PID=$!

echo "[entrypoint] backend pid=$BACKEND_PID (127.0.0.1:8000, loopback-only), frontend pid=$FRONTEND_PID (0.0.0.0:${PORT:-3000})"

# See the top-of-file note on why this isn't just `wait -n "$BACKEND_PID" "$FRONTEND_PID"`
# under `set -e`. Treating its result as data (via `if`), not a script error, is the fix.
if wait -n "$BACKEND_PID" "$FRONTEND_PID"; then
  EXIT_CODE=0
else
  EXIT_CODE=$?
fi

echo "[entrypoint] a process exited (code $EXIT_CODE) — tearing down the other."
exit "$EXIT_CODE"
