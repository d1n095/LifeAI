#!/bin/bash
# Consolidated Cursor correction gate. Usage: run.sh <candidate-worktree> <built-frontend-dir>
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; P="$(cd "$HERE/.." && pwd)"; T="$(cd "$1" && pwd)"; FE="$(cd "$2" && pwd)"
export ACCEPT_VENV="${ACCEPT_VENV:-$P/../../venv312/bin/python}"
SHA="$(git -C "$T" rev-parse HEAD)"; OUT="$HERE/out-${SHA:0:7}"; rm -rf "$OUT"; mkdir -p "$OUT"; echo "candidate $SHA"
REDIS_URL=redis://127.0.0.1:6390/4 "$P/app_readiness_v1/run.sh" "$T" -rA --tb=line -p no:warnings \
  -k "AR9 or AR11 or AR13 or AR14 or AR15 or AR17 or AR18" > "$OUT/app.log" 2>&1
REDIS_URL=redis://127.0.0.1:6390/5 "$P/stop_classification_v1/run.sh" "$T" -rA --tb=line -k "FO6 or RL or FC_ or untyped" > "$OUT/stop.log" 2>&1
{ "$P/proxy_v1/run.sh" "$FE"; "$P/proxy_v1/xff_ratelimit.sh" "$T" "$FE"; "$P/proxy_v1/identity_collapse.sh" "$T" "$FE"; } > "$OUT/proxy.log" 2>&1
if [ -d "$FE/../pw-browsers" ]; then "$P/ui_v1/run.sh" "$T" "$FE" > "$OUT/ui.log" 2>&1 < /dev/null; else echo "FAILED ui::BROWSER_MISSING" > "$OUT/ui.log"; fi
python3 - "$OUT" <<'PY'
import re, sys, pathlib
out = pathlib.Path(sys.argv[1]); res = {}
for f in out.glob("*.log"):
    for m in re.finditer(r"^(PASSED|FAILED|ERROR|SKIPPED) (\S+)", f.read_text(errors="replace"), re.M):
        res[m.group(2)] = m.group(1)
GATES = {"R1 (P1)": ["PX-9_", "PX-10_", "PX-11_", "PX-9a", "PX-10a"], "P-1": ["PX-1_"], "R2": ["AR18a", "AR18b", "AR18c", "AR18d"],
         "D1": ["AR11_", "AR11b"], "D2": ["AR9_concurrent", "AR9h"], "R3": ["AR9f"], "R4": ["test_FC_"], "R5": ["FO6b", "FO6x", "test_RL_", "untyped"],
         "R6": ["AR13c", "AR13_", "AR13b"], "D4": ["UI-1", "UI-2"], "D5": ["AR17", "UI-3"], "N1": ["AR14a"], "N2/N3": ["AR14b", "AR14d"], "N4": ["AR15"]}
bad = 0
for g, pats in GATES.items():
    hit = {k: v for k, v in res.items() if any(p in k for p in pats)}
    missing = [p for p in pats if not any(p in k for k in hit)]
    ok = hit and all(v == "PASSED" for v in hit.values()) and not missing
    bad += not ok
    fails = [k.split("::")[-1] for k, v in hit.items() if v != "PASSED"]
    print(f"{'PASS' if ok else 'FAIL'} {g:8s} {'' if ok else 'failing=' + str(fails) + (' missing=' + str(missing) if missing else '')}")
print(f"CORRECTION GATE: {'PASS' if bad == 0 else 'FAIL'} ({bad} defect gate(s) open)")
PY
