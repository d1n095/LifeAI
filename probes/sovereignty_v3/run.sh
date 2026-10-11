#!/bin/bash
# Founder Sovereignty adversarial suite (examiner-owned). Runs from OUTSIDE the target, loads target conftest as plugin.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"
VENV="$HERE/../../venv312/bin/python"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"
BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_sov_v3_$$" \
  "$VENV" -m pytest -q -s -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null test_sovereignty_v3.py "${@:2}"
RC=$?
AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }
exit $RC
