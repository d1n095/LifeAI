# ACTIVE WORK — Cursor Autonomy Night Shift

**Owner:** Cursor (Autonomy Activation / B5–B7 lane)  
**Started:** 2026-08-26  
**Integration tip at claim:** `4c86e58` (#154 merged — EVER_GOVERNED fail-closed)

## Claimed now

| Surface | Branch / PR | Purpose |
|---|---|---|
| Provider-spend foundation | `cursor/provider-spend-authorization` #155 | Land 0060 + call-boundary; rebase onto tip; merge when green |
| Plan-scope narrowing helper | `cursor/plan-derived-scope-narrowing` #156 | Fix CI twin flake (unrelated storage race); rebase; do **not** wire at WorkBinding build |
| B5 goal rollup / next-task | TBD `cursor/goal-rollup-next-task` | Production proof + adversarial attacks |
| B6 repair across ticks | TBD after B5 | Durable repair loop |
| B7 wait/wake | TBD after B6 | Production wake edges |

## Locked / do not touch

- Claude-owned cognition / unrelated open lanes
- `provider_spend_authorized` / `remote_write_authorized` remain **false** in production_entry until explicit founder authorization (night-shift standing order)
- Final post-ACCEPT narrowing wire into OperatorContext — after #155 lands; separate from WorkBinding construction

## Standing rules

- One concern per PR when practical
- Do not rebase “for safety” ahead of real dependencies
- Verify reviewed head == merge head
- Migration single-head: tip head is **0059**; #155 adds **0060**
