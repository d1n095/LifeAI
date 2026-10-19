#!/bin/bash
# Run #286's acceptance harness copy EXCLUDING test_credential_isolation.py (CRED probes are not
# executed by this examiner), inside the host-credential-denying sandbox, with a CANARY global
# git helper in the outer env. Verifies the canary is never invoked and its secret never persisted.
# Usage: run_harness_sandboxed.sh <target-worktree> <out-name>
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; OUT="$HERE/out/$2"; VENV="$HERE/../../venv312/bin/python"
WORK="$(mktemp -d)"; CH="$WORK/canary-home"; mkdir -p "$CH"
SECRET="CANARY-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
printf '#!/bin/sh\ntouch "%s/canary-invoked"; cat >/dev/null; echo username=canary; echo password=%s\n' "$WORK" "$SECRET" > "$WORK/canary-helper.sh"; chmod +x "$WORK/canary-helper.sh"
printf '[credential]\n\thelper = !%s\n' "$WORK/canary-helper.sh" > "$CH/.gitconfig"
sed "s#\$HOME#$HOME#g" "$HERE/deny_host_creds.sb.template" > "$WORK/profile.sb"
SHA="$(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
cd "$HERE/h286" && sandbox-exec -f "$WORK/profile.sb" env -u GH_TOKEN -u GITHUB_TOKEN -u GIT_ASKPASS -u SSH_AUTH_SOCK \
  HOME="$CH" GIT_CONFIG_GLOBAL="$CH/.gitconfig" GIT_CONFIG_NOSYSTEM=1 \
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERE/h286:$TARGET/backend" SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="exfc286_$$" \
  REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6390/0}" FIRST_CYCLE_ACCEPTANCE_SHA="$SHA" FIRST_CYCLE_ACCEPTANCE_MATRIX="$OUT.json" \
  "$VENV" -m pytest -q -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$HERE/h286" -c /dev/null first_cycle_acceptance \
  --ignore=first_cycle_acceptance/test_credential_isolation.py > "$OUT.log" 2>&1
tail -1 "$OUT.log"
echo "canary-invoked: $([ -e "$WORK/canary-invoked" ] && echo YES || echo no)"
echo "secret-in-outputs: $(grep -l "$SECRET" "$OUT.json" "$OUT.log" 2>/dev/null | wc -l | tr -d ' '); secret-in-target: $(grep -rl "$SECRET" "$TARGET" 2>/dev/null | wc -l | tr -d ' ')"
echo "repo-copy-written: $([ -e "$HERE/h286/docs/examiner/first_cycle_acceptance_matrix.json" ] || [ -e "$HERE/docs/examiner/first_cycle_acceptance_matrix.json" ] && echo YES || echo no)"
AFTER="$(git -C "$TARGET" status --porcelain)"; [ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" || echo "WARNING: target worktree changed"
rm -rf "$WORK"
