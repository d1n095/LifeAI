#!/bin/bash
# Run the Grandmaster verification suite against a target worktree without modifying it.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"
VENV="$HERE/../../venv312/bin/python"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"
BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  "$VENV" -m pytest -q -p no:cacheprovider -W ignore --rootdir="$HERE" -c /dev/null -rA test_grandmaster_regression.py "${@:2}"
RC=$?
AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }
exit $RC
