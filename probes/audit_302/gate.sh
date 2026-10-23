#!/bin/bash
# Combined release security acceptance gate — see RELEASE_SECURITY_GATE.md.
# Usage: gate.sh <target-worktree>   (e.g. gate.sh ../../wtcert-ec0f4d2)
# Classifies the release into Scope A (first supervised safe cycle), Scope B (unattended autonomy),
# Scope C (vendor activation). Scope A must PASS; Scope B requires the two Codex blockers CLOSED
# (so this gate expects CO-10 / FAC to still FAIL today and reports Scope B BLOCKED accordingly).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; P="$HERE/.."
TARGET="${1:?usage: gate.sh <target-worktree>}"
pass=0; fail=0; A_ok=1

run_mustpass () { # name dir
  local name="$1" dir="$2"
  if "$P/$dir/run.sh" "$TARGET" >/tmp/gate_$$.log 2>&1 && grep -qE "[0-9]+ passed" /tmp/gate_$$.log && ! grep -qE "[1-9][0-9]* failed" /tmp/gate_$$.log; then
    echo "  PASS  $name ($(grep -oE '[0-9]+ passed' /tmp/gate_$$.log | tail -1))"; pass=$((pass+1))
  else
    echo "  FAIL  $name  <-- Scope A must-pass regressed"; fail=$((fail+1)); A_ok=0
  fi
}

expect_blocker_open () { # name dir keyword
  local name="$1" dir="$2" kw="$3"
  "$P/$dir/run.sh" "$TARGET" >/tmp/gate_$$.log 2>&1
  if grep -qE "[1-9][0-9]* failed" /tmp/gate_$$.log && grep -q "$kw" /tmp/gate_$$.log; then
    echo "  OPEN  $name (blocker still present — Scope B stays BLOCKED)"
  else
    echo "  CLOSED? $name — re-verify: the blocker suite no longer fails; confirm the fix before clearing Scope B"
  fi
}

echo "== Scope A — first Founder-supervised SAFE cycle (must-pass) =="
run_mustpass "security_audit (SEC-1..4)" security_audit_v1
run_mustpass "gh_intake (incl GH-4)"     gh_intake_v1
run_mustpass "delivery"                  delivery_v1
run_mustpass "connect"                   connect_v1
run_mustpass "lifecycle"                 lifecycle_v1
run_mustpass "handover"                  handover_v1
run_mustpass "live_handover"             live_handover_v1
run_mustpass "security_diag"             security_diag_v1
run_mustpass "cognitive_reliability (CR-01..20)" cognitive_v1

echo "== Scope B — UNATTENDED autonomy (requires both Codex blockers CLOSED) =="
expect_blocker_open "FA-9/CO-10 (STOP-clear)" coord_accept_v1 "CO10"
expect_blocker_open "FA-PRIV (mainai_app DB authority)" fa_core_v1 "FAC"

echo "== Scope C — external vendor activation =="
echo "  BLOCKED  vendor messaging usable=False (agent_messaging_adapter); needs agent-side runner + Founder token"

rm -f /tmp/gate_$$.log
echo "---"
if [ "$A_ok" = 1 ] && [ "$fail" = 0 ]; then
  echo "RESULT: Scope A PASS ($pass must-pass suites). Scope B BLOCKED (Codex FA-9/CO-10 + FA-PRIV). Scope C BLOCKED."
  echo "=> Cleared for the first Founder-supervised safe cycle only. NOT unattended autonomy, NOT vendor activation."
  exit 0
else
  echo "RESULT: Scope A FAILED ($fail must-pass regressions) — release blocked at the supervised-cycle gate."
  exit 1
fi
