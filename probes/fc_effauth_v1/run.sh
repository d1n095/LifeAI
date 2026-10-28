#!/bin/bash
# Effect-Specific Database Authority examination (migration 0099), DB-backed via tests.conftest.
# Drives the REAL privileged security_kernel_revoke_execution_envelope function, the envelope
# REVOKE/UPDATE privilege surface, and the append-only audit triggers on the exact candidate SHA.
# Network-free beyond loopback Postgres/Redis (egress guard); no paid calls; no production mutation.
# Usage: run.sh <target-worktree> [test files] [pytest args]   (no files = every test_fc_effauth*.py)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"; VENV="${ACCEPT_VENV:-$HERE/../../../venv312/bin/python}"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
FILES=(); ARGS=(); for a in "${@:2}"; do case "$a" in test_fc_effauth*) FILES+=("$a");; *) ARGS+=("$a");; esac; done
[ ${#FILES[@]} -eq 0 ] && FILES=($(cd "$HERE" && ls test_fc_effauth*.py))
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_ASKPASS -u SSH_AUTH_SOCK -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  -u GEMINI_API_KEY -u DEEPSEEK_API_KEY -u OPENROUTER_API_KEY \
  HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  PATH="$(dirname "$VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_fceff_$$" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/6}" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null -s \
  "${FILES[@]}" "${ARGS[@]+"${ARGS[@]}"}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
