#!/bin/bash
# Run a copy of the first-cycle acceptance harness (h279 = original #279 ceb416e, h284 = #284's copy)
# against a target backend. Usage: run_harness.sh <h279|h284> <target-worktree> <out-name> [pytest args]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; H="$HERE/$1"; TARGET="$(cd "$2" && pwd)"; OUT="$HERE/out/$3"; VENV="$HERE/../../venv312/bin/python"
SHA="$(git -C "$TARGET" rev-parse HEAD)"; echo "harness=$1 target=$SHA"; BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$H" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$H:$TARGET/backend" SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="exfc_$$" \
  FIRST_CYCLE_ACCEPTANCE_SHA="$SHA" FIRST_CYCLE_ACCEPTANCE_MATRIX="$OUT.json" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$H" -c /dev/null first_cycle_acceptance "${@:4}" > "$OUT.log" 2>&1
RC=$?; tail -3 "$OUT.log"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
