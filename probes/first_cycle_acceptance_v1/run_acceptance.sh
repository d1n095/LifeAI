#!/bin/bash
# First-cycle independent acceptance run.
#   run_acceptance.sh <cursor|codex|combined> <candidate-worktree> [--quick]
# cursor = Cursor execution/application successor; codex = Codex composed security candidate;
# combined = Codex integration candidate judged for application + activation readiness (every suite required).
# Proxy suites need a built frontend: FE_BUILT=<dir from proxy_v1/build_frontend.sh>; without it they report FAILED (never skipped).
# Network-free except the read-only, unauthenticated GitHub API read of exact-SHA CI (suite ci_exact_sha).
# Synthetic credentials only; hermetic git; host credential helpers denied by sandbox for credential suites.
set -u
PROFILE="${1:?profile cursor|codex|combined}"; TARGET="$(cd "${2:?candidate worktree}" && pwd)"; QUICK="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"; PROBES="$(cd "$HERE/.." && pwd)"
export ACCEPT_VENV="${ACCEPT_VENV:-$PROBES/../../venv312/bin/python}"
[ -x "$ACCEPT_VENV" ] || { echo "ACCEPT_VENV not executable: $ACCEPT_VENV"; exit 2; }
SHA="$(git -C "$TARGET" rev-parse HEAD)"; OUT="$HERE/out/$PROFILE-${SHA:0:7}"; rm -rf "$OUT"; mkdir -p "$OUT"
export REAL_HOME="$HOME"; BEFORE="$(git -C "$TARGET" status --porcelain)"
source "$HERE/hermetic_env.sh"
echo "profile=$PROFILE target=$SHA out=$OUT"

# 1 STOP / failure classification (FO, LR, RS)
Q1=(); [ "$QUICK" = "--quick" ] && Q1=(-k "pre_receive or opaque or LR1")
"$PROBES/stop_classification_v1/run.sh" "$TARGET" -rA --tb=line "${Q1[@]+"${Q1[@]}"}" > "$OUT/stop_classification.log" 2>&1
# 2 credential isolation canary (sandboxed; needs REAL_HOME only to build the deny profile)
env HOME="$REAL_HOME" "$PROBES/stop_classification_v1/run_canary.sh" "$TARGET" > "$OUT/credential_canary.log" 2>&1
# 2b production transport credential sources (TC-0..TC-3; canary helper, sandbox, localhost 401 server)
env HOME="$REAL_HOME" REAL_HOME="$REAL_HOME" "$PROBES/transport_contract_v1/run.sh" "$TARGET" -rA --tb=line > "$OUT/transport_contract.log" 2>&1
# 3 review receipts / STOP-before-merge / goal STOP / replan (DB-level, real worker ticks)
Q3=(); [ "$QUICK" = "--quick" ] && Q3=(-k "S1_ or S7_ or N1b")
"$PROBES/fc286/run.sh" "$TARGET" -rA --tb=line "${Q3[@]+"${Q3[@]}"}" > "$OUT/review_stop.log" 2>&1
# 4 candidate's own acceptance harness, CRED file excluded, sandbox + canary
env HOME="$REAL_HOME" "$HERE/run_candidate_harness_sandboxed.sh" "$TARGET" "$OUT" > /dev/null 2>&1
# 5 Path A task authority / publication content (TA-8b..TA-13) and spend (SPEND-*)
"$PROBES/e2e_v1/run.sh" "$TARGET" test_task_authority_273.py test_task_authority_276.py -k "${QUICK:+ta10 and }(ta or rename or symlink or intermediate)" > "$OUT/task_authority.log" 2>&1
"$PROBES/path_a_v1/run.sh" "$TARGET" test_path_a_268.py test_path_a_270.py test_path_a_276_ctx.py -k "not unseen_existing and not large_file" > "$OUT/spend.log" 2>&1
# 6 authorization / grants / account erasure (Codex security candidate)
if [ "$PROFILE" = "codex" ] || [ "$PROFILE" = "combined" ]; then
  "$PROBES/activation_280/run.sh" "$TARGET" -rA --tb=line > "$OUT/authorization_grants.log" 2>&1
  # adapter-driven grant / remote-effect contract (GC-1..GC-8); BLOCKED / ADAPTER-NEEDED never pass
  "$PROBES/grant_contract_v1/run.sh" "$TARGET" -rA --tb=line > "$OUT/grant_contract.log" 2>&1
fi
# 7 candidate's own backend regression (selected domains)
( cd "$TARGET/backend" && F=$(ls tests/backend/*.py tests/backend/mainai/*.py | grep -E "first_cycle|ci_wait|path_a|replan|recovery|executor|execution_job|mainai_execution|worker|envelope|verification|account_erasure|migration_roundtrip|provider_spend|advance_tasks|takeover|retry|wait|ci_workflow|staging|activation|github_client" | tr '\n' ' ')
  [ "$QUICK" = "--quick" ] && F="tests/backend/test_ci_workflow_contract.py tests/backend/test_migration_roundtrip.py"
  SECRET_KEY=x TZ=UTC LIFEAI_TEST_DATABASE_NAME="accept_reg_$$" "$ACCEPT_VENV" -m pytest -q -rA -p no:cacheprovider -W ignore $F ) > "$OUT/backend_regression.log" 2>&1
# 8 FC-4 npm-audit policy (offline unit + bypass matrix)
( cd "$TARGET/frontend" && node --test scripts/check-npm-audit.test.js ) > "$OUT/fc4_policy.log" 2>&1
( cd "$PROBES/fc4_npm_audit_282" && POLICY="$TARGET/frontend/scripts/check-npm-audit.js" node --test bypass_policy.test.js ) > "$OUT/fc4_bypass.log" 2>&1
# 10 application readiness: route authz matrix, Founder-ops STOP/truthfulness/isolation, duplicate-work races, safe launch
REDIS_URL="${REDIS_URL%/*}/3" "$PROBES/app_readiness_v1/run.sh" "$TARGET" -rA --tb=line > "$OUT/app_readiness.log" 2>&1
# 11 same-origin proxy: traversal, header trust, XFF rate-limit bypass end to end (real backend with Dockerfile flags)
if [ -n "${FE_BUILT:-}" ] && [ -d "$FE_BUILT/.next" ]; then
  { "$PROBES/proxy_v1/run.sh" "$FE_BUILT"; "$PROBES/proxy_v1/xff_ratelimit.sh" "$TARGET" "$FE_BUILT"; "$PROBES/proxy_v1/identity_collapse.sh" "$TARGET" "$FE_BUILT"; } > "$OUT/proxy.log" 2>&1
else
  echo "FAILED proxy::FRONTEND_NOT_BUILT (set FE_BUILT from proxy_v1/build_frontend.sh)" > "$OUT/proxy.log"
fi
# 12 migration integrity (single head, unique ids/prefixes, linear chain; catches the dual-0089 composition)
"$ACCEPT_VENV" "$PROBES/migration_integrity_v1/check.py" "$TARGET/backend" > "$OUT/migration_integrity.log" 2>&1
# 13 UI truthfulness in a real browser (D4 stale status, D5 blocker count) against a live local stack
if [ -n "${FE_BUILT:-}" ] && [ -d "$FE_BUILT/../pw-browsers" ]; then
  env HOME="$REAL_HOME" "$PROBES/ui_v1/run.sh" "$TARGET" "$FE_BUILT" > "$OUT/ui.log" 2>&1 < /dev/null
else
  echo "FAILED ui::BROWSER_OR_FRONTEND_MISSING (FE_BUILT with ../pw-browsers required)" > "$OUT/ui.log"
fi
# 9 exact-SHA CI (read-only, unauthenticated public API; no credentials)
"$ACCEPT_VENV" - "$SHA" > "$OUT/ci_exact_sha.json" <<'PY'
import json, sys, urllib.request
sha = sys.argv[1]
REQ = ["Frontend — TypeScript & ESLint", "Frontend — npm audit", "Frontend — build (docker)", "Frontend — build (vercel)",
       "Backend — unit/integration tests", "Backend — RLS & session-security tests", "Backend — account lifecycle & rate-limit tests",
       "Backend — Alembic migration check", "E2E — Playwright (full stack)", "E2E — Library / Founder Knowledge Studio",
       "E2E — same-origin proxy", "All required checks passed"]
try:
    req = urllib.request.Request(f"https://api.github.com/repos/d1n095/LifeAI/commits/{sha}/check-runs?per_page=100",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "examiner-acceptance"})
    runs = json.load(urllib.request.urlopen(req, timeout=20))["check_runs"]
    out = {name: sorted((r["conclusion"] or r["status"]) for r in runs if r["name"] == name) for name in REQ}
    print(json.dumps({"sha": sha, "required": out}, ensure_ascii=False, indent=1))
except Exception as exc:
    print(json.dumps({"sha": sha, "error": f"{type(exc).__name__}: {exc}"}))
PY
AFTER="$(git -C "$TARGET" status --porcelain)"; [ "$BEFORE" == "$AFTER" ] && echo "target worktree unchanged" | tee "$OUT/target_unchanged.txt" || echo "WARNING: target worktree changed" | tee "$OUT/target_unchanged.txt"
rm -rf "$ACCEPT_HOME"
"$ACCEPT_VENV" "$HERE/summarize.py" "$OUT" "$PROFILE" | tee "$OUT/summary.txt"
