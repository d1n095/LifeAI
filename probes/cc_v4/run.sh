#!/bin/bash
# Continuous Conversation P1-fix-4 examiner suite. Runs outside the target; loads target conftest; checks target unchanged.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"; VENV="$HERE/../../venv312/bin/python"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="examiner_cc_v4_$$" \
  "$VENV" -m pytest -q -s -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null test_cc_logic.py test_cc_db.py "${@:2}"
RC=$?; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
