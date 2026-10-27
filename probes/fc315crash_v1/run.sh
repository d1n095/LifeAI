#!/bin/bash
# #315 crash-consistency & finalized-file integrity. Pure filesystem store + fault injection;
# NO DB, NO tests.conftest, NO network, no paid calls. Usage: run.sh <target-worktree> [files] [pytest args]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "${1:?usage: run.sh <target-worktree>}" && pwd)"
VENV="${ACCEPT_VENV:-$HERE/../../../venv312/bin/python}"
echo "target: $TARGET @ $(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
FILES=(); ARGS=(); for a in "${@:2}"; do case "$a" in test_*) FILES+=("$a");; *) ARGS+=("$a");; esac; done
[ ${#FILES[@]} -eq 0 ] && FILES=($(cd "$HERE" && ls test_fc315crash*.py))
SANDBOX_HOME="$(mktemp -d)"
cd "$HERE" && env -u GH_TOKEN -u GITHUB_TOKEN -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u GEMINI_API_KEY \
  HOME="$SANDBOX_HOME" PATH="$(dirname "$VENV"):$PATH" PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x TZ=UTC \
  "$VENV" -m pytest -q -p no:cacheprovider -W ignore --rootdir="$HERE" -c /dev/null -s \
  "${FILES[@]}" "${ARGS[@]+"${ARGS[@]}"}"
RC=$?; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target changed"; RC=99; }; exit $RC
