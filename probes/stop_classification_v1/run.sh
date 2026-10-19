#!/bin/bash
# STOP / failure-classification regression package (modules A-C). Network-free, credential-free.
# Usage: run.sh <target-worktree> [pytest args]   (module D runs separately via run_canary.sh)
# Needs: local Postgres on :5433, Redis (REDIS_URL), the backend venv. Git runs hermetically:
# temp HOME, GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1, no token/askpass/agent variables.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree> [pytest args]}" && pwd)"; VENV="$HERE/../../venv312/bin/python"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_ASKPASS -u SSH_AUTH_SOCK \
  HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  GIT_AUTHOR_NAME=examiner GIT_AUTHOR_EMAIL=examiner@test.local GIT_COMMITTER_NAME=examiner GIT_COMMITTER_EMAIL=examiner@test.local \
  PATH="$(dirname "$VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_stopcls_$$" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/0}" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null \
  test_a_failure_origins.py test_b_local_retries.py test_c_running_sibling.py "${@:2}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
