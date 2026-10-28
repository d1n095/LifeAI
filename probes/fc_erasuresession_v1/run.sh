#!/bin/bash
# Priority 8 — Revoked Founder Session vs Account-Erasure Authority. DB-backed via
# tests.conftest. Drives the REAL Founder session-revocation primitives
# (reset_founder_password_with_token, revoke_all_sessions_for_user -> revoke_current_founder_sessions)
# and the REAL account_erasure_* / personal_recall_erasure_current gating, then measures whether
# a just-revoked session still satisfies erasure authority and can drive an actual protected-data
# delete, on the exact candidate SHA. Deterministic (fixed stored timestamps; no wall-clock race).
# RECALL STAYS INACTIVE. Loopback-only (egress guard); no paid calls; no production mutation.
# Usage: run.sh <target-worktree> [test files] [pytest args]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"; VENV="${ACCEPT_VENV:-$HERE/../../../venv312/bin/python}"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
FILES=(); ARGS=(); for a in "${@:2}"; do case "$a" in test_fc_erasuresession*) FILES+=("$a");; *) ARGS+=("$a");; esac; done
[ ${#FILES[@]} -eq 0 ] && FILES=($(cd "$HERE" && ls test_fc_erasuresession*.py))
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_ASKPASS -u SSH_AUTH_SOCK -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  -u GEMINI_API_KEY -u DEEPSEEK_API_KEY -u OPENROUTER_API_KEY \
  HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  PERSONAL_RECALL_SYSTEM_KEK_B64="$(printf 'r%.0s' {1..32} | base64)" PERSONAL_RECALL_SYSTEM_KEK_VERSION=test-v1 \
  PATH="$(dirname "$VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_fcerasesess_$$" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/15}" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null -s \
  "${FILES[@]}" "${ARGS[@]+"${ARGS[@]}"}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
