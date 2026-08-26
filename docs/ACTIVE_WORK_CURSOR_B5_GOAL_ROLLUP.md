# ACTIVE WORK — Cursor Autonomy Night Shift (B5)

**Owner:** Cursor  
**Branch:** `cursor/goal-rollup-next-task`  
**Integration tip at branch create:** `4c86e58` (#154)

## Claimed now

| Surface | Status |
|---|---|
| `backend/tests/backend/mainai/test_goal_rollup_next_task_production.py` | **writing/landing** — B5 production rollup/next-task proof |

## Not claimed / do not touch

- Claude-owned cognition lanes
- `provider_spend_authorized` / `remote_write_authorized` remain **false** in `production_entry`
- #155 provider-spend foundation merge (separate worktree)
- #156 plan-scope narrowing (separate worktree)

## Evidence (this unit)

- Local: `8 passed` on `test_goal_rollup_next_task_production.py`
- Chain proven: finalize gate → readiness → `record_final_report` → `eligible_authorized_goals` → bind order
- Attacks: crash before recompute, duplicate recompute, failed→blocked, terminal drop, waiting wake, revoke, multi-ready priority

## Remaining uncertainty

- Ordinary `production_entry` ticks still stop at `PROVIDER_SPEND_NOT_AUTHORIZED` without spend or explicit PlanCandidate — intentional under night-shift spend=false; not papered over here.
- B6/B7 still open after this lands.
