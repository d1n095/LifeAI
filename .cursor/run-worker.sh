#!/usr/bin/env bash
# Restart-on-crash wrapper for the durable worker in Cloud Agent terminals.
#
# Cursor Cloud Agent terminals are tmux panes that do not auto-restart when the
# command exits. If the worker dies (OOM, uncaught exception that escapes the
# poll-loop guard, transient DB unavailability at startup), Life's imports, MainAI
# jobs, and storage-deletion outbox go permanently dead for the rest of the session.
#
# This wrapper retries with capped exponential backoff (1s → max 30s) and logs each
# restart so the founder can see crash frequency in the terminal scrollback.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO/backend"
# shellcheck disable=SC1091
source .venv/bin/activate

MAX_DELAY=30
delay=1
consecutive_failures=0

while true; do
  echo "[run-worker] Starting app.worker at $(date -Iseconds)"
  python -m app.worker
  exit_code=$?

  if [ $exit_code -eq 0 ]; then
    echo "[run-worker] Worker exited cleanly (code 0) — graceful shutdown, not restarting."
    break
  fi

  consecutive_failures=$((consecutive_failures + 1))
  echo "[run-worker] Worker exited with code $exit_code (crash #$consecutive_failures). Restarting in ${delay}s..."
  sleep "$delay"

  # Exponential backoff capped at MAX_DELAY
  delay=$((delay * 2))
  if [ "$delay" -gt "$MAX_DELAY" ]; then
    delay=$MAX_DELAY
  fi
done
