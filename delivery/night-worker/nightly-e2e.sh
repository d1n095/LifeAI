#!/bin/bash
# Nightly end-to-end run against a LOCAL, isolated LifeAI stack.
#
# Drives the full founder-authorized chain with no chat session involved:
#   goal (autonomous_development_work) -> AI plan -> founder approval of every
#   repo_edit task -> execution scope proposal -> authorized envelope ->
#   bounded provider-spend grant -> worker tick -> isolated per-goal worktree ->
#   focused test -> task events/checkpoints -> goal rollup.
#
# Nothing here merges, deploys, pushes or touches the shared checkout: the
# supervisor executes only inside /tmp/mainai-supervisor-goal-worktrees/<goal_id>.
#
# Usage:
#   API=http://127.0.0.1:8000 \
#   FOUNDER_EMAIL=founder@lifeai.local FOUNDER_PASSWORD=... \
#   ./nightly-e2e.sh
#
# Exit codes: 0 all tasks completed, 1 anything else.

set -euo pipefail
API="${API:-http://127.0.0.1:8000}"
FOUNDER_EMAIL="${FOUNDER_EMAIL:-founder@lifeai.local}"
FOUNDER_PASSWORD="${FOUNDER_PASSWORD:?FOUNDER_PASSWORD must be set}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

json() { python3 -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1"; }

echo "== login =="
CSRF=$(curl -sf -c "$JAR" -H 'Content-Type: application/json' \
  -X POST "$API/api/auth/login" \
  -d "{\"email\":\"$FOUNDER_EMAIL\",\"password\":\"$FOUNDER_PASSWORD\"}" \
  | json "d['csrf_token']")
AUTH=(-b "$JAR" -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF")

echo "== goal =="
GOAL=$(curl -sf "${AUTH[@]}" -X POST "$API/api/mainai/execution/goals" -d '{
  "title":"Nattlig utvecklingsuppgift: subtract-helper",
  "original_instruction":"Lägg till en subtract-funktion i calculator.py och ett focused test som verifierar den. Endast calculator.py och test_calculator.py får ändras.",
  "risk_level":"low",
  "approval_policy":"autonomous_development_work"}' | json "d['id']")
echo "goal=$GOAL"

echo "== plan (local model, ~45 s) =="
curl -sf "${AUTH[@]}" -X POST "$API/api/mainai/execution/goals/$GOAL/plan" >/dev/null

echo "== founder approval of every repo_edit task =="
curl -sf -b "$JAR" "$API/api/mainai/execution/goals/$GOAL" \
  | json "' '.join(t['id'] for t in d['tasks'] if t['task_type']=='repo_edit')" \
  | tr ' ' '\n' | while read -r task; do
      [ -z "$task" ] && continue
      curl -sf "${AUTH[@]}" -X POST "$API/api/mainai/execution/tasks/$task/approve" >/dev/null
      echo "approved $task"
    done

echo "== execution scope proposal + founder authorization =="
PATHS='["calculator.py","test_calculator.py"]'
CAPS='["create_file","patch_file","read_file","list_files","run_focused_test"]'
PROPOSAL=$(curl -sf "${AUTH[@]}" -X POST "$API/api/execution-envelopes/goals/$GOAL/propose" \
  -d "{\"proposed_paths\":$PATHS,\"proposed_capabilities\":$CAPS,\"proposed_risk\":\"low\"}" | json "d['id']")
curl -sf "${AUTH[@]}" -X POST "$API/api/execution-envelopes/proposals/$PROPOSAL/authorize" \
  -d "{\"authorized_paths\":$PATHS,\"authorized_capabilities\":$CAPS,\"authorized_risk\":\"low\"}" >/dev/null
echo "envelope authorized"

echo "== bounded provider-spend grant (local ollama only) =="
ENVELOPE=$(curl -sf -b "$JAR" "$API/api/execution-envelopes/current?goal_id=$GOAL" | json "d['id']")
curl -sf "${AUTH[@]}" -X POST "$API/api/provider-spend/authorize" -d "{
  \"goal_id\":\"$GOAL\",
  \"execution_envelope_id\":\"$ENVELOPE\",
  \"max_cost_usd\":1.00,
  \"max_requests\":50,
  \"max_cost_per_request_usd\":0.20,
  \"allowed_providers\":[\"ollama\"],
  \"allowed_models\":[\"llama3.1\"],
  \"idempotency_key\":\"nightly-e2e-$GOAL\"}" >/dev/null
echo "spend grant active (ceiling 1.00 USD / 50 requests)"

echo "== watching the worker (no chat involved) =="
DEADLINE=$(( $(date +%s) + TIMEOUT_SECONDS ))
while :; do
  OUT=$(curl -sf -b "$JAR" "$API/api/mainai/execution/goals/$GOAL")
  GOAL_STATUS=$(printf '%s' "$OUT" | json "d['status']")
  printf '  %s\n' "$(printf '%s' "$OUT" | json "' '.join(f\"{t['task_type']}={t['status']}\" for t in d['tasks'])")"
  case "$GOAL_STATUS" in
    completed) echo "GOAL COMPLETED"; exit 0 ;;
    failed|cancelled|blocked) echo "GOAL $GOAL_STATUS"; exit 1 ;;
  esac
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then echo "TIMEOUT"; exit 1; fi
  sleep 10
done
