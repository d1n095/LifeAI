#!/bin/bash
# Transport contract probes (TC-0..TC-3). Sandboxed: real host credential stores and helper binaries are
# denied; the only helper is a per-run canary. Network: localhost HTTP server only.
# Usage: run.sh <target-worktree> [pytest args]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; VENV="${ACCEPT_VENV:-$HERE/../../venv312/bin/python}"
REAL="${REAL_HOME:-$HOME}"; WORK="$(mktemp -d)"; CH="$WORK/canary-home"; mkdir -p "$CH"
printf '#!/bin/sh\ntouch "%s/canary-invoked"; cat >/dev/null; echo username=canary; echo password=CANARY\n' "$WORK" > "$WORK/canary-helper.sh"; chmod +x "$WORK/canary-helper.sh"
printf '[credential]\n\thelper = !%s\n' "$WORK/canary-helper.sh" > "$CH/.gitconfig"
sed "s#\$HOME#$REAL#g" "$HERE/deny_host_creds.sb.template" > "$WORK/profile.sb"
BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && sandbox-exec -f "$WORK/profile.sb" env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_AUTH_SOCK \
  HOME="$CH" GIT_CONFIG_GLOBAL="$CH/.gitconfig" GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
  EXAMINER_CANARY_MARKER="$WORK/canary-invoked" EXAMINER_CANARY_HELPER="$WORK/canary-helper.sh" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE:$TARGET/backend" SECRET_KEY=x \
  "$VENV" -m pytest -q -p no:cacheprovider -W ignore --rootdir="$HERE" -c /dev/null test_transport_contract.py "${@:2}"
RC=$?; rm -rf "$WORK"; AFTER="$(git -C "$TARGET" status --porcelain)"
[ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || { echo "WARNING: target worktree changed"; RC=99; }; exit $RC
