#!/usr/bin/env bash
# Unattended CI entrypoint for the isolated LifeAI integration test lab.
# No production resource is referenced. Set TESTLAB_KEEP=1 to inspect the lab after failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
cd "$ROOT"

export TESTLAB_EVIDENCE_DIR="${TESTLAB_EVIDENCE_DIR:-$ROOT/delivery/v7-testlab/evidence}"
DURATION_SECONDS="${TESTLAB_DURATION_SECONDS:-14400}"
mkdir -p "$TESTLAB_EVIDENCE_DIR"

cleanup() {
  if [ "${TESTLAB_KEEP:-0}" = "1" ]; then
    echo "TESTLAB_KEEP=1: leaving the lab running" >&2
    return
  fi
  "$PYTHON" scripts/testlab/testlab.py down >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "== preflight =="
"$PYTHON" scripts/testlab/preflight.py --mode docker --out "$TESTLAB_EVIDENCE_DIR/preflight.json"

echo "== isolated lab up =="
"$PYTHON" scripts/testlab/testlab.py up

echo "== founder acceptance =="
"$PYTHON" scripts/testlab/testlab.py acceptance

echo "== reliability soak (${DURATION_SECONDS}s) =="
"$PYTHON" scripts/testlab/testlab.py soak --duration-seconds "$DURATION_SECONDS"

echo "== fail-closed gate =="
"$PYTHON" scripts/testlab/testlab.py gate
