# ACTIVE WORK — Claude (crash/recovery authority fencing)

**Owner:** Claude
**Branch:** `claude/recovery-authority-fencing`
**Integration tip at branch create:** `8574ab1` (#164)
**Rebased onto:** `e8e4fee` (#155 merged, provider-spend + migration 0060) — migration
renumbered 0060 → 0061 (`down_revision = "0060"`). The original PR #165 head (`d47b8c37`) and
its CI run are OBSOLETE -- do not treat that run as the merge gate; the rebased head is what
actually goes to CI/merge.

## Claimed now

| Surface | Purpose |
|---|---|
| `backend/app/mainai_execution/recovery_takeover.py` | `execute_takeover()` declines V0.1 dispatch for any EVER_GOVERNED goal |
| `backend/app/jobs/mainai_job_lease.py` | New `mark_job_failed_after_governed_recovery_decline()` sibling to `mark_job_superseded()` |
| `backend/app/execution_envelopes/service.py` | New `goal_has_ever_been_envelope_governed()` helper |
| `backend/app/models/mainai_recovery.py` | New `MainAIRecoveryEventType.takeover_declined_governed` |
| `backend/alembic/versions/0061_recovery_takeover_governed_decline.py` | Allows the new event_type value |
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

## First-governance TOCTOU fence (added after founder review)

`goal_has_ever_been_envelope_governed()` alone is a plain SELECT -- a founder authorizing a
goal's FIRST-EVER envelope concurrently with a recovery pass for that same goal could still
race: recovery reads EVER_GOVERNED=false, the envelope commits a moment later, recovery
(unaware) proceeds to dispatch through V0.1 anyway. Closed with a real structural fence, not
another SELECT: both `execute_takeover()` and `authorize_execution_scope()` now take
`SELECT ... FOR UPDATE` on the SAME `MainAIGoal` row before their own consequential decision,
serializing the two orderings (whichever gets there first fully completes before the other
proceeds) so "governance becomes effective mid-flight of an already-decided legacy dispatch"
is structurally impossible. Two real-thread, real-DB-connection regression tests prove both
orderings, with genuine blocking verified via a join timeout (not simulated). Negative-control
verified: temporarily removing `execute_takeover()`'s lock makes the critical-direction test
fail exactly as expected; removing `authorize_execution_scope()`'s own explicit lock does NOT
by itself break the test, because `ExecutionScopeProposal`/`ExecutionAuthorizationEnvelope`'s
own composite FK to `mainai_goals` already takes an implicit `FOR KEY SHARE` lock on that row
on insert -- real defense-in-depth, but the explicit lock is kept anyway since a later
`authorize_execution_scope()` call against an already-existing, already-committed proposal
gets none of that incidental protection from the proposal side, and relying on an undocumented
FK side-effect for a security invariant would be fragile.

## Downgrade-with-real-data proof

`alembic downgrade -1`'s re-narrowed CHECK constraint was verified against a GENUINE
`takeover_declined_governed` row (inserted via the real `execute_takeover()` code path
against a persistent DB, not a hand-rolled INSERT) -- it fails loudly (`IntegrityError`/
`CheckViolation`), by design: `mainai_recovery_events` is append-only at the database level
(migration 0033's trigger denies UPDATE unconditionally, no GUC escape hatch), so there is no
safe way to rewrite/remove that row to make the downgrade succeed, and the migration must
never try. Documented explicitly in migration 0061's own docstring +
`test_downgrading_past_this_migration_fails_loudly_once_a_real_row_exists_not_silently`.

## Evidence

- Local: 18 adversarial tests (`test_recovery_takeover_authority_fencing.py`) -- 16 passed
  outright, 2 fail locally on the SAME pre-existing local-Postgres-timezone artifact that also
  fails an unmodified, already-merged baseline test in `test_mainai_execution_auto_recovery.py`
  (confirmed by running that baseline test unmodified -- it fails identically, proving this is
  environmental, not a regression from this change).
- Full regression (`tests/backend/mainai/`, `tests/security/`, `tests/backend/jobs/`, +
  worktree/exclusion/production_worktree/recovery-family files): running against the rebased
  head.
- `ruff check` on all touched files: clean.
- `alembic upgrade head` / `downgrade -1` (empty DB) / `upgrade head` round-trip: clean, single
  head (0061), 0060 (provider-spend) intact.
