# First Autonomous Task — Blocker Map

Live map for Cursor's **Autonomy Activation Lane**. Ranked by whether the item blocks
the first honest bounded local autonomous task — not by cosmetic completeness.

Tip at map creation: `cf4999a`. Claude owns `EVER_GOVERNED + NO_ACTIVE => STOP` (#154).
Those files remain **locked** until her merge.

Target chain:

```text
founder-authorized goal
→ current execution envelope
→ founder provider-spend grant (API)
→ Supervisor trigger
→ reserve spend → provider plan → settle spend
→ Safe Planner validation + ACCEPT
→ narrow paths/caps from accepted plan ∩ envelope
→ local Operator/Driver execution
→ focused verification
→ task/goal state propagation
→ recovery/lesson
→ next job
```

## Ranked blockers

| ID | Blocks first real task? | Status | Owner | Notes |
|---|---|---|---|---|
| **B1** | **YES** | CORRECTNESS FIX IN FLIGHT (#155) | Cursor | Reserve→invoke→settle at real `plan_with_provider` boundary; owner-scoped `source_ref`; one-active partial unique; full authority idempotency; founder `/api/provider-spend`; alembic downgrade fixed. **Do NOT wire** `production_entry` until #154 lands + attack. |
| **B2** | YES (authority) | Claude #154 open | Claude | `EVER_GOVERNED + NO_ACTIVE => STOP`. Attack after merge. |
| **B3** | YES (until production_entry wire) | HARNESS UPDATED | Cursor | E2E now asserts settled usage (`spent_requests==1`), not just boolean→call. |
| **B4** | YES (over-broad write) | HELPER READY (#156) | Cursor | Wire **after Safe Planner ACCEPT**, not at WorkBinding build. Docs corrected. |
| **B5** | Partial | OPEN | Cursor | Task completion proven; goal rollup / next-task via production_entry still open. |
| **B6** | Repair loop | OPEN | Cursor | Failed verification → bounded repair → resume across ticks. |
| **B7** | Resilience | OPEN | Cursor | Outage → WAITING_PROVIDER; budget exhaust fail-closes; wake/re-grant path still open. |
| **B8** | No | PARKED | Cursor | 0057 SET NULL cleanup — **not** primary; wait until MainAI actually runs. |

## Locked (do not write)

- `backend/app/worker.py`
- `backend/tests/backend/test_advance_tasks_excludes_envelope_governed_goals.py`
- `docs/LIFE_SUPERVISOR_PRODUCTION_ENTRY.md`
- Final `provider_spend_authorized=` in `production_entry.py`

## After Claude merges B2

1. Attack her invariant.
2. Rebase #155/#156.
3. Wire `provider_spend_authorized = provider_spend_is_live(...)`.
4. Wire post-ACCEPT plan narrowing into OperatorContext.
5. Composed production_entry E2E until one genuine local task completes without human PlanCandidate.
