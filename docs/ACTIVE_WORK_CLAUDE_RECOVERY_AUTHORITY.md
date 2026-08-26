# ACTIVE WORK — Claude (crash/recovery authority fencing)

**Owner:** Claude
**Branch:** `claude/recovery-authority-fencing`
**Integration tip at branch create:** `8574ab1` (#164)

## Claimed now

| Surface | Purpose |
|---|---|
| `backend/app/mainai_execution/recovery_takeover.py` | `execute_takeover()` declines V0.1 dispatch for any EVER_GOVERNED goal |
| `backend/app/jobs/mainai_job_lease.py` | New `mark_job_failed_after_governed_recovery_decline()` sibling to `mark_job_superseded()` |
| `backend/app/execution_envelopes/service.py` | New `goal_has_ever_been_envelope_governed()` helper |
| `backend/app/models/mainai_recovery.py` | New `MainAIRecoveryEventType.takeover_declined_governed` |
| `backend/alembic/versions/0060_recovery_takeover_governed_decline.py` | Allows the new event_type value |
| `backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py` | Adversarial proofs |

Not touched: `backend/app/worker.py`, `backend/app/routers/mainai_execution.py` (both callers of
`execute_takeover()` inherit the fix through the shared function — no change needed there),
none of Cursor's #155/#156/#160/#162/#164 files.

## Standing boundaries

- `provider_spend_authorized=false`
- `remote_write_authorized=false`
- Do not touch Cursor-owned lanes (B1/B4/B5/B6/B7)

## Root cause

PR #157's own "Residual (documented, not fixed here)" note: `execute_takeover()` / auto-recovery
remains execution-envelope blind. `dispatch_ready_task()` runs a `task_execution` job through
`app.mainai_execution.execution_job.run_task_execution_job()` -- zero `OperatorContext`/
`SupervisorScope` awareness anywhere in that module. PR #154 closed this gap for
`app.worker.py`'s OWN blanket auto-dispatch tick, but `execute_takeover()` is a second,
independent caller of the same `dispatch_ready_task()`, reachable both from the automatic
`_advance_mainai_execution_auto_recovery()` tick and from a founder's own
`POST /tasks/{id}/recover` -- neither was covered.

## Fix

`execute_takeover()` now checks `goal_has_ever_been_envelope_governed()` (mirrors PR #154's own
"mere row existence, any status" EVER_GOVERNED predicate) immediately after the existing
approval gate. For an EVER_GOVERNED goal it takes a new `_decline_takeover_for_governed_goal()`
branch: the task returns to `ready` (no fabricated verdict, same as an ordinary takeover's own
step 1) but no V0.1 job is dispatched and no salvage runs; the dead job is finalized `failed`
(not `superseded` -- no 1:1 successor to record) via the new
`mark_job_failed_after_governed_recovery_decline()`. The goal's OWN next
`run_authorized_goal_supervisor_tick()` (driven automatically by
`app.worker.py`'s `_advance_authorized_supervisor_goals`) rediscovers the now-`ready` task and
redispatches it through `prepare_context()`'s real `OperatorContext` binding, re-validated
against whatever the CURRENT active envelope authorizes at that later moment.

## Evidence

- Local: 15 new adversarial tests (`test_recovery_takeover_authority_fencing.py`) -- 13 passed
  outright, 2 fail locally on the SAME pre-existing local-Postgres-timezone artifact that also
  fails an unmodified, already-merged baseline test in `test_mainai_execution_auto_recovery.py`
  (confirmed by running that baseline test unmodified -- it fails identically, proving this is
  environmental, not a regression from this change).
- Full regression (`tests/backend/mainai/`, `tests/security/`, `tests/backend/jobs/`, +
  worktree/exclusion/production_worktree/recovery-family files): running.
- `ruff check` on all touched files: clean.
- `alembic upgrade head` / `downgrade -1` / `upgrade head` round-trip: clean, single head (0060).
