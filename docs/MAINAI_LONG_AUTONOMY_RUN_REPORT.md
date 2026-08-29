# MainAI — Long Autonomy Run Report (Stage 3)

**Branch:** `cursor/long-autonomy-soak-v1`  
**Proof test:** `backend/tests/backend/mainai/test_composed_autonomy_soak_v4_long_autonomy.py`  
**Program:** `docs/ACTIVE_WORK_CURSOR_MAINAI_V1_COMPLETION_RUN.md`  
**Authenticity spec:** `docs/MAINAI_V1_READINESS.md` Part A (from Claude design lane #197)

## What ran

Eight sequential `repo_edit` tasks on a disposable calculator worktree
(`multiply` → `divide` → `subtract` → `power` → `modulo` → `absolute` → `negate` →
`minimum`), plus one production-created repair child after a verification failure.

Orchestration: Worker → `run_authorized_goal_supervisor_tick` only. Fake provider adapter
is the sole fake boundary.

## Exercises proven

| Requirement | How |
|---|---|
| Provider planning | Sequenced `_SoakPlanningAdapter` / `RegistryPlanningAdapter` |
| Deterministic local work | Real Operator write/verify on worktree |
| Task dependencies | `depends_on` chain; later tasks stay pending while A blocked |
| Transient provider failure | `ProviderError(rate_limited)` → park → Worker wake |
| Out-of-scope plan denied | Deny payload; no `outside_envelope.py` |
| Verification → gap → repair | Broken multiply → `LifeProblem` + `_repair_child` → recipe repair → reverify |
| Process/session restart | Session A closed; Session B reloads by durable UUIDs only |
| Lease takeover | Crash-hold `supervisor_goal_leases` → B blocked → wall-clock expiry → B reclaim |
| Final rollup | Worker `_finalize_mainai_execution_goals`; `goal.final_outcome` set |
| Idle ticks | Three post-complete ticks; calculator bytes + mtime unchanged |

## Forbidden harness bridges (not used)

- No hand `PlanCandidate` / repair task / WorkBinding after start
- No `task.status = …` mutations after start
- No manual dependency unlock
- No direct `record_final_report`
- No `run_driver` as top-level orchestration
- No shared ORM Session across the restart boundary

## Founder edges (explicit)

- Bootstrap: claim → authorize work candidate → plan → task approvals → envelope → spend
- One mid-run `grant_task_approval` for the repair child's `repo_edit`
- Bootstrap `max_attempts=12` so spend/rate-limit/broken-verify attempts do not exhaust the
  default of 3 before repair re-verification (production attempt accounting unchanged)

## Outcome

Goal reaches `completed` with all eight planned tasks and the repair child terminal. Correct
`multiply` formula present; all subsequent helpers present; no out-of-envelope files.

## Follow-ups (not blocking Stage 3)

- Expand to 10–12 helpers if desired for soak duration only (mechanism already covered at 8).
- Stage 4 bounded self-improvement uses a separate acceptance contract
  (`docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md`) — not this calculator fixture.
