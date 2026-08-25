# First Autonomous Task — Blocker Map

Live map for Cursor's **Autonomy Activation Lane**. Ranked by whether the item blocks
the first honest bounded local autonomous task — not by cosmetic completeness.

Tip at map creation: `cf4999a`. Claude owns `EVER_GOVERNED + NO_ACTIVE => STOP`
(`revocation-fallback-fix` dirty: `worker.py`,
`test_advance_tasks_excludes_envelope_governed_goals.py`,
`LIFE_SUPERVISOR_PRODUCTION_ENTRY.md`). Those files are **locked**.

Target chain:

```text
founder-authorized goal
→ current execution envelope
→ Supervisor trigger
→ bounded provider-assisted planning   ← PRIMARY BLOCKER (foundation landed; production_entry wire deferred)
→ Safe Planner validation
→ local Operator/Driver execution
→ focused verification
→ task/goal state propagation
→ recovery/lesson
→ next job
```

## Ranked blockers

| ID | Blocks first real task? | Status | Owner | Notes |
|---|---|---|---|---|
| **B1** | **YES — hard stop** | FOUNDATION READY (this lane PR) | Cursor | Migration **0060** + `app.provider_spend` service: distinct founder grant, ceilings, allowlists, expiry/revoke, idempotent usage, FOR UPDATE accounting, erasure. `provider_spend_is_live()` is the boolean `production_entry` will call. **Do NOT wire** `production_entry.py` until Claude unlocks. |
| **B2** | YES (authority) | Claude active | Claude | `EVER_GOVERNED + NO_ACTIVE_ENVELOPE => STOP`. Cursor #152 characterized latent V0.1 reopen. Do not touch. Attack after her merge. |
| **B3** | YES (until production_entry wire) | HARNESS LANDED | Cursor | E2E: envelope alone → `PROVIDER_SPEND_NOT_AUTHORIZED`; live spend grant → fake provider → Safe Planner → Driver completes local edit. Still bypasses `production_entry` by design. |
| **B4** | Likely | OPEN — inspect | Cursor | After accepted plan: task-level path/capability narrowing from validated plan without treating planner output as founder authority. Envelope remains ceiling; plan may only narrow. |
| **B5** | Likely | OPEN — inspect | Cursor | Verification completion → task + goal state on production Supervisor path (E2E above proves task completion via `run_supervisor`; goal rollup / next-task still needs production_entry tick proof). |
| **B6** | Repair loop | OPEN | Cursor | Failed verification → bounded repair → resume across ticks (worktree + lease + job claim). |
| **B7** | Resilience | OPEN | Cursor | Provider outage / budget exhaustion → wakeable deferred state. Outage already yields `WAITING_PROVIDER` under spend-authorized scope; budget exhaustion now fail-closes spend grant — need wake/re-grant path, not permanent stall with no clock. |
| **B8** | No (cleanup) | PARKED | Cursor | Migration 0057 composite `ON DELETE SET NULL` on `supersedes_envelope_id` with `owner_id NOT NULL` — CI idle only. 0060 already uses column-specific SET NULL. |

## Locked (do not write)

- `backend/app/worker.py`
- `backend/tests/backend/test_advance_tasks_excludes_envelope_governed_goals.py`
- `docs/LIFE_SUPERVISOR_PRODUCTION_ENTRY.md`
- Final `provider_spend_authorized=` assignment in `production_entry.py` until after Claude merge + attack

## Done this lane so far

- [x] `docs/FIRST_AUTONOMOUS_TASK_BLOCKER_MAP.md`
- [x] Migration 0060 `provider_spend_authorizations` + append-only `provider_spend_usage_events`
- [x] `app/provider_spend` service (`authorize` / `revoke` / `is_live` / `record_usage`)
- [x] RLS + erasure wiring (spend erase **before** envelope/goal CASCADE)
- [x] Unit + erasure tests
- [x] Production-shaped E2E harness (fake provider, real gates, no production_entry wire)

## After Claude merges B2

1. Refresh tip + attack her invariant (adversarial cases from founder).
2. Rebase this lane.
3. Wire smallest `production_entry` edge: `provider_spend_authorized = provider_spend_is_live(...)`.
4. Optionally call `record_provider_spend_usage` from `plan_with_provider` (durable budget, not just UsageLog).
5. Run composed autonomous-task E2E through production_entry.
6. Fix what breaks; continue until one genuine bounded local task completes without human PlanCandidate translation.
