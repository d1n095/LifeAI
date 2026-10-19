#!/bin/bash
# Run the CANDIDATE's own first-cycle acceptance harness (archived from the target's HEAD, so the
# candidate tree is never written to), EXCLUDING test_credential_isolation.py, inside the
# host-credential-denying sandbox with a CANARY global git helper. Usage: <target-worktree> <out-dir>
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; TARGET="$(cd "$1" && pwd)"; OUT="$2"; mkdir -p "$OUT"
WORK="$(mktemp -d)"; CH="$WORK/canary-home"; mkdir -p "$CH" "$WORK/h"
git -C "$TARGET" archive HEAD backend/tests/backend/first_cycle_acceptance | tar -x -C "$WORK/h" --strip-components=3
SECRET="CANARY-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
printf '#!/bin/sh\ntouch "%s/canary-invoked"; cat >/dev/null; echo username=canary; echo password=%s\n' "$WORK" "$SECRET" > "$WORK/canary-helper.sh"; chmod +x "$WORK/canary-helper.sh"
printf '[credential]\n\thelper = !%s\n' "$WORK/canary-helper.sh" > "$CH/.gitconfig"
sed "s#\$HOME#$REAL_HOME#g" "$HERE/../stop_classification_v1/deny_host_creds.sb.template" > "$WORK/profile.sb"
SHA="$(git -C "$TARGET" rev-parse HEAD)"; BEFORE="$(git -C "$TARGET" status --porcelain)"
( cd "$WORK/h" && sandbox-exec -f "$WORK/profile.sb" env HOME="$CH" GIT_CONFIG_GLOBAL="$CH/.gitconfig" GIT_CONFIG_NOSYSTEM=1 \
  PYTHONPATH="$WORK/h:$TARGET/backend" SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="accept_h_$$" \
  FIRST_CYCLE_ACCEPTANCE_SHA="$SHA" FIRST_CYCLE_ACCEPTANCE_MATRIX="$WORK/matrix.json" \
  "$ACCEPT_VENV" -m pytest -q -rA -p no:cacheprovider -p tests.conftest -W ignore --rootdir="$WORK/h" -c /dev/null first_cycle_acceptance \
  --ignore=first_cycle_acceptance/test_credential_isolation.py ) > "$OUT/harness.log" 2>&1
RC=$?
{ echo "canary_invoked=$([ -e "$WORK/canary-invoked" ] && echo yes || echo no)"
  echo "secret_in_outputs=$(grep -l "$SECRET" "$WORK/matrix.json" "$OUT/harness.log" 2>/dev/null | wc -l | tr -d ' ')"
  echo "secret_in_target=$(grep -rl "$SECRET" "$TARGET" 2>/dev/null | wc -l | tr -d ' ')"
  echo "target_unchanged=$([ "$BEFORE" == "$(git -C "$TARGET" status --porcelain)" ] && echo yes || echo no)"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("matrix_go=%s counts=%s" % (d["go"], d["counts"])); [print("matrix_row", r["status"], r["test_id"]) for r in d["rows"] if r["status"]!="PASS"]' "$WORK/matrix.json" 2>/dev/null || echo "matrix=missing"
} > "$OUT/harness_safety.txt"
rm -rf "$WORK"; exit $RC
