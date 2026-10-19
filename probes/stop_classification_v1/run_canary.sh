#!/bin/bash
# Credential-isolation verification for #286 using a CANARY helper only. Never touches host helpers:
# runs under a sandbox profile denying the real ~/.gitconfig, gh config, keychains and helper binaries.
# Usage: run_canary.sh <target-worktree> [extra pytest target...]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; VENV="$HERE/../../venv312/bin/python"
WORK="$(mktemp -d)"; CH="$WORK/canary-home"; mkdir -p "$CH"
SECRET="CANARY-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
cat > "$WORK/canary-helper.sh" <<H
#!/bin/sh
touch "$WORK/canary-invoked"; cat >/dev/null; echo "username=canary"; echo "password=$SECRET"
H
chmod +x "$WORK/canary-helper.sh"
printf '[credential]\n\thelper = !%s\n' "$WORK/canary-helper.sh" > "$CH/.gitconfig"
sed "s#\$HOME#$HOME#g" "$HERE/deny_host_creds.sb.template" > "$WORK/profile.sb"
BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE" && sandbox-exec -f "$WORK/profile.sb" env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_AUTH_SOCK \
  HOME="$CH" GIT_CONFIG_GLOBAL="$CH/.gitconfig" GIT_CONFIG_NOSYSTEM=1 \
  EXAMINER_CANARY_SECRET="$SECRET" EXAMINER_CANARY_MARKER="$WORK/canary-invoked" EXAMINER_CANARY_HELPER="$WORK/canary-helper.sh" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$TARGET/backend/tests/backend:$TARGET/backend" \
  "$VENV" -m pytest -q -p no:cacheprovider -W ignore --rootdir="$HERE" -c /dev/null test_d_credential_extra_env.py "${@:2}" -rA 2>&1 | grep -E "^(PASSED|FAILED|ERROR) |AssertionError|Error|passed|failed" | head -20
echo "secret-in-target-tree: $(grep -rl "$SECRET" "$TARGET" 2>/dev/null | wc -l | tr -d ' ')"
AFTER="$(git -C "$TARGET" status --porcelain)"; [ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || echo "WARNING: target worktree changed"
rm -rf "$WORK"
