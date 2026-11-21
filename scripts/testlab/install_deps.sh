#!/usr/bin/env bash
# Reproducible host/CI dependencies for the LifeAI integration test lab.
# Safe to rerun. Docker Engine itself is a documented prerequisite, not installed here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"

echo "== Python test dependencies =="
"$PYTHON" -m pip install -r "$ROOT/backend/requirements-dev.txt"

echo "== Frontend dependencies =="
cd "$ROOT/frontend"
npm ci

echo "== Playwright Chromium and system libraries =="
if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
  echo "Playwright system dependencies require root or sudo." >&2
  echo "Run: cd frontend && npx playwright install --with-deps chromium" >&2
  exit 3
fi
npx playwright install --with-deps chromium

echo "Dependencies installed. Run: $PYTHON scripts/testlab/preflight.py"
