#!/bin/bash
# Build a candidate's frontend OUTSIDE its frozen worktree (so the worktree stays byte-identical).
# Usage: build_frontend.sh <candidate-worktree> <out-dir>   -> prints the built frontend dir
# Needs the npm registry (package-lock integrity hashes; install scripts disabled). No credentials, telemetry off.
set -eu
TARGET="$(cd "$1" && pwd)"; OUT="$2"; SHA="$(git -C "$TARGET" rev-parse HEAD)"
rm -rf "$OUT"; mkdir -p "$OUT"
git -C "$TARGET" archive "$SHA" frontend | tar -x -C "$OUT"
cd "$OUT/frontend"
H="$(mktemp -d)"; env -i PATH="$PATH" HOME="$H" NEXT_TELEMETRY_DISABLED=1 npm ci --ignore-scripts --no-audit --no-fund >/dev/null 2>&1
env -i PATH="$PATH" HOME="$H" NEXT_TELEMETRY_DISABLED=1 npx next build > ../build.log 2>&1
rm -rf "$H"; echo "$OUT/frontend"
