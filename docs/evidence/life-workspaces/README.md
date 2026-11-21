# Life Workspaces — exact-SHA evidence

**Code checkpoint:** `83c87049646cf2a52bc1baeb0527776b99fe5a85`
**Branch:** `freebuff/life-workspaces`
**Migrations:** `0093_life_workspaces`, `0094_workspace_sync_and_supervised_goals`

## Results

| Check | Result |
| --- | --- |
| Workspace API + project detail + chat idempotency | **15 passed, 0 failed** |
| MainAI execution API + planner regression | **58 passed, 0 failed** |
| Frontend lint | **PASS**, 0 errors, 1 pre-existing warning |
| Frontend production build | **PASS**, both workspace routes generated |
| Chromium workspace journeys | **2 expected / 0 unexpected / 0 flaky** |

## Browser journeys

`frontend/e2e/workspaces.spec.ts` executed against real Next.js, real FastAPI, real
PostgreSQL and the existing deterministic E2E provider:

1. **Persistent workspace:** log in, create a workspace with purpose/goal, add task and
   decision, stream a MainAI reply, reload with conversation/task/decision persisted, save a
   versioned settings edit, run a sync, archive and restore.
2. **Failure recovery:** inject a transient `503` on workspace create, verify the real error
   alert and absence of a phantom card, retry, and verify the workspace appears.

## Artifacts

- `backend-tests.xml` / `backend-tests.log` — JUnit and text output for the 15 focused tests.
- `mainai-execution-regression.log` — the 58-test execution API/planner regression.
- `frontend-lint.txt` / `frontend-build.txt` — lint and production build.
- `playwright.json` / `playwright-summary.json` — raw and summarized browser result.
- `summary.json` — machine-readable verdict.

## Limits

The browser uses the existing deterministic E2E provider, not a real external model. Sync is a
bounded reconciliation ledger over existing durable state, not a second ingestion engine. The
workspace context is owner-scoped and is not an execution authorization. No production
deployment, push, PR, merge, force-push or migration-lane rewrite was performed.
`first_cycle_go=false`.
