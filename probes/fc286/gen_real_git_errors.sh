#!/bin/bash
# Produce REAL git push stderr for local, credential-free failure modes, formatted exactly like
# app/mainai_execution/worktree.py::_run_git ("git <args> failed (<rc>): <stderr>").
set -u
OUT="$1"; T="$(mktemp -d)"; export HOME="$T/h" GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0; mkdir -p "$HOME"
g() { git -c user.name=x -c user.email=x@x -c init.defaultBranch=main "$@"; }
cd "$T"; g init -q --bare remote.git; g init -q w; cd w; g commit -q --allow-empty -m a; g remote add origin ../remote.git
g push -q origin main:b
g commit -q --allow-empty --amend -m a2; NFF="$(g push origin main:b 2>&1)"; NFFRC=$?
printf '#!/bin/sh\necho "push not authorized" >&2\nexit 1\n' > ../remote.git/hooks/pre-receive; chmod +x ../remote.git/hooks/pre-receive
HOOK="$(g push origin main:c 2>&1)"; HOOKRC=$?; rm ../remote.git/hooks/pre-receive
g remote set-url origin "$T/missing.git"; MISS="$(g push origin main:b 2>&1)"; MISSRC=$?
python3 - "$OUT" "$T" "$NFFRC" "$NFF" "$HOOKRC" "$HOOK" "$MISSRC" "$MISS" <<'PY'
import json,sys
out,t=sys.argv[1:3]; a=sys.argv[3:]
c=lambda s:s.replace(t,"<tmp>")
fmt=lambda rc,s,ref:f"git push origin {ref} failed ({rc}): {c(s)}"
json.dump({"non_fast_forward":fmt(a[0],a[1],"claude/mainai-task-x:claude/mainai-task-x"),
           "pre_receive_declined":fmt(a[2],a[3],"claude/mainai-task-x:claude/mainai-task-x"),
           "remote_unreachable":fmt(a[4],a[5],"claude/mainai-task-x:claude/mainai-task-x")},open(out,"w"),indent=1)
PY
rm -rf "$T"
