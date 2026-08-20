#!/usr/bin/env bash
# Restart-on-crash wrapper for the FastAPI/uvicorn backend in Cloud Agent terminals.
#
# Same rationale as run-worker.sh: Cursor Cloud Agent terminals do not auto-restart.
# If uvicorn exits (OOM, C-extension segfault, startup failure after services were ready),
# the API goes permanently dead for the session. The health endpoint also dies, making
# the failure visible externally — but there is no automatic recovery without this wrapper.
#
# Backoff is capped at 30s. Exit 0 (graceful SIGTERM shutdown) does NOT trigger restart.
# After 5 consecutive failures with no successful startup, the wrapper stops to avoid
# masking a deterministic bug (e.g. broken migration state).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO/backend"
# shellcheck disable=SC1091
source .venv/bin/activate

MAX_DELAY=30
MAX_CONSECUTIVE_FAILURES=5
delay=1
consecutive_failures=0

while true; do
  echo "[run-backend] Starting uvicorn at $(date -Iseconds)"
  start_time=$(date +%s)
  uvicorn app.main:app --host 0.0.0.0 --port 8000
  exit_code=$?
  end_time=$(date +%s)
  run_duration=$((end_time - start_time))

  if [ $exit_code -eq 0 ]; then
    echo "[run-backend] Uvicorn exited cleanly (code 0) — graceful shutdown, not restarting."
    break
  fi

  consecutive_failures=$((consecutive_failures + 1))

  if [ "$consecutive_failures" -ge "$MAX_CONSECUTIVE_FAILURES" ]; then
    echo "[run-backend] $MAX_CONSECUTIVE_FAILURES consecutive failures — likely a deterministic bug. Stopping." >&2
    exit 1
  fi

  # If it ran for more than 60s, reset backoff (it was serving successfully before crashing)
  if [ "$run_duration" -gt 60 ]; then
    delay=1
    consecutive_failures=1
  fi

  echo "[run-backend] Uvicorn exited with code $exit_code (crash #$consecutive_failures). Restarting in ${delay}s..."
  sleep "$delay"

  delay=$((delay * 2))
  if [ "$delay" -gt "$MAX_DELAY" ]; then
    delay=$MAX_DELAY
  fi
done
