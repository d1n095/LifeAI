#!/bin/bash
# Run the Orchestration Truth Ledger verification suite against a target worktree without modifying it.
# Pure-logic tests need nothing; DB tests load the target's own backend conftest as a plugin (local PG on 5433).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"
VENV="$HERE/../../venv312/bin/python"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"
BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_ledger_v2_$$" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null -rA \
  test_ledger_logic.py test_ledger_db.py "${@:2}"
RC=$?
AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }
exit $RC
