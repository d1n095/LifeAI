#!/bin/bash
# Application-readiness adversarial suite (backend side). Network-free (egress guard), no paid calls.
# Usage: run.sh <target-worktree> [test files] [pytest args]   (no test files = every test_ab*.py)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"; VENV="${ACCEPT_VENV:-$HERE/../../../venv312/bin/python}"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
FILES=(); ARGS=(); for a in "${@:2}"; do case "$a" in test_ab*) FILES+=("$a");; *) ARGS+=("$a");; esac; done
[ ${#FILES[@]} -eq 0 ] && FILES=($(cd "$HERE" && ls test_ab*.py))
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_ASKPASS -u SSH_AUTH_SOCK -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  -u GEMINI_API_KEY -u DEEPSEEK_API_KEY -u OPENROUTER_API_KEY \
  HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  GIT_AUTHOR_NAME=examiner GIT_AUTHOR_EMAIL=examiner@test.local GIT_COMMITTER_NAME=examiner GIT_COMMITTER_EMAIL=examiner@test.local \
  PATH="$(dirname "$VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$HERE/../stop_classification_v1:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_ab_$$" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/3}" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null -s \
  "${FILES[@]}" "${ARGS[@]+"${ARGS[@]}"}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
