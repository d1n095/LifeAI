#!/bin/bash
# Examiner probes for #280 activation authority. Loads the target's conftest; verifies the target is unchanged.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree> [pytest args]}" && pwd)"; VENV="$HERE/../../venv312/bin/python"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="examiner_act280_$$" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null test_activation_280_exam.py "${@:2}"
RC=$?; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
