#!/bin/bash
# Run a copy of the first-cycle acceptance harness (h279 = original #279 ceb416e, h284 = #284's copy)
# against a target backend. Usage: run_harness.sh <h279|h284> <target-worktree> <out-name> [pytest args]
#
# SAFETY (#284 P1-3): the harness's CRED-4 probe runs `git credential fill` and records the raw
# output. Run with the host git configuration, it disclosed a live token. This runner therefore:
#   - refuses to run unless EXAMINER_ALLOW_HARNESS=1 is set explicitly;
#   - always deselects test_credential_isolation.py (CRED-1..5) — those rows are NOT produced;
#   - isolates git from the host: empty temp HOME, GIT_CONFIG_GLOBAL=/dev/null,
#     GIT_CONFIG_NOSYSTEM=1, GIT_TERMINAL_PROMPT=0, no GH_TOKEN/GITHUB_TOKEN/GIT_ASKPASS/SSH_AUTH_SOCK.
# The published out/*.json matrices predate this hardening and are redacted.
set -u
[ "${EXAMINER_ALLOW_HARNESS:-}" = "1" ] || { echo "refusing: set EXAMINER_ALLOW_HARNESS=1 to run the (credential-probe-excluded) harness"; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"; H="$HERE/$1"; TARGET="$(cd "$2" && pwd)"; OUT="$HERE/out/$3"; VENV="$HERE/../../venv312/bin/python"
SHA="$(git -C "$TARGET" rev-parse HEAD)"; echo "harness=$1 target=$SHA"; BEFORE="$(git -C "$TARGET" status --porcelain)"
SANDBOX_HOME="$(mktemp -d)"
cd "$H" && env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_ASKPASS -u SSH_AUTH_SOCK \
  HOME="$SANDBOX_HOME" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$H:$TARGET/backend" SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="exfc_$$" \
  FIRST_CYCLE_ACCEPTANCE_SHA="$SHA" FIRST_CYCLE_ACCEPTANCE_MATRIX="$OUT.json" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$H" -c /dev/null first_cycle_acceptance \
  --ignore=first_cycle_acceptance/test_credential_isolation.py "${@:4}" > "$OUT.log" 2>&1
RC=$?; tail -3 "$OUT.log"; rm -rf "$SANDBOX_HOME"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
