#!/bin/bash
# Priority 9 — Independent Regression Gate. Encodes the REQUIRED session-revocation + timestamp
# invariant across Founder authority, account erasure and a privileged session-gated effect.
# The erasure requirements are xfail(strict) keyed to the confirmed Priority-8 defect: they report
# expected-fail on the broken build and become XPASS->failure when the one-line fix lands.
# DB-backed via tests.conftest; drives the REAL reset / authority helpers on the exact SHA.
# Loopback-only (egress guard); no paid calls; no production mutation. RECALL INACTIVE.
# Usage: run.sh <target-worktree> [test files] [pytest args]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"; VENV="${ACCEPT_VENV:-$HERE/../../../venv312/bin/python}"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
FILES=(); ARGS=(); for a in "${@:2}"; do case "$a" in test_fc_sessionreg*) FILES+=("$a");; *) ARGS+=("$a");; esac; done
[ ${#FILES[@]} -eq 0 ] && FILES=($(cd "$HERE" && ls test_fc_sessionreg*.py))
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_ASKPASS -u SSH_AUTH_SOCK -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  -u GEMINI_API_KEY -u DEEPSEEK_API_KEY -u OPENROUTER_API_KEY \
  HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  PERSONAL_RECALL_SYSTEM_KEK_B64="$(printf 'r%.0s' {1..32} | base64)" PERSONAL_RECALL_SYSTEM_KEK_VERSION=test-v1 \
  PATH="$(dirname "$VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  LIFEAI_TEST_DATABASE_NAME="examiner_fcsessreg_$$" REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/15}" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE" -c /dev/null -s -rX \
  "${FILES[@]}" "${ARGS[@]+"${ARGS[@]}"}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
