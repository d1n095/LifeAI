#!/bin/bash
# Grant / remote-effect contract tests against a candidate. Hermetic, network-free.
# Usage: run.sh <target-worktree> [pytest args]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"; VENV="${ACCEPT_VENV:-$HERE/../../venv312/bin/python}"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_AUTH_SOCK HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
  GIT_TERMINAL_PROMPT=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_grant_$$" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/0}" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null test_grant_contract.py "${@:2}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
