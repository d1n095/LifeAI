# MainAI V1 — Stage 2 (takeover) and Stage 3 (long-run authenticity) adversarial prep

Companion to `docs/MAINAI_V1_READINESS.md` and `docs/ACTIVE_WORK_CURSOR_MAINAI_V1_COMPLETION_
RUN.md`. Prepared ahead of Cursor's Stage 2/3 PRs, per the same discipline applied to every
correction-pass phase and Stage 1 (#202) — attack criteria plus, where practical, a real
empirical regression already built and merged, not just a checklist.

## Stage 2 — lease expiry + takeover continuation

### Already covered, do not re-litigate without new evidence

- **Sequential simulation**: `test_operator_lease_effect_time_race.py`'s
  `test_expired_job_lease_blocks_write_with_zero_filesystem_effect` and
  `test_takeover_generation_bump_blocks_stale_worker_write_zero_fs_effect` — same-session
  mutation before the call, proves the CHECK logic, not real concurrency (#184).
- **Scope-narrowing after takeover**: `test_local_write_crash_before_verify.py`'s
  `test_second_worker_resuming_after_lease_takeover_cannot_heal_a_write_outside_its_own_
  current_scope` (#191) — a second worker with a narrower scope can't inherit a crashed
  write's broader authority.
- **Effect-fence freshness**: `_require_context()` now owns `populate_existing=True` for
  job/task (#199) — a plain `select()` no longer serves stale identity-mapped attributes
  within the SAME session.

### New this session, real two-connection race, merged

`test_operator_lease_effect_time_race.py`'s
`test_genuine_cross_session_takeover_mid_run_blocks_next_step_zero_effect` — the first GENUINE
two-connection takeover test in this family (every prior one mutated the SAME session before
the call). Worker A's Driver run completes step 1; a truly separate session commits a real
takeover between steps; worker A's own next step attempt, still using its stale in-memory
`OperatorContext`, is checked.

**Result: safety holds** (zero filesystem effect from the superseded step, confirmed via
`run_driver()`'s own between-steps `db.refresh(job)` correctly observing the separate
session's commit under READ COMMITTED — not the same-session staleness class #199 fixed, a
genuinely different and now also-verified mechanism).

**CLOSED (was a real finding, not a security gap, now fixed on `v1-readiness-workstream`)**:
`run_driver()`'s per-step `try/except` previously only caught `OperatorCapabilityMissing`
gracefully; `OperatorAuthorizationError` from a mid-run takeover propagated *uncaught* out of
`run_driver()` instead of becoming a clean `DriverResult`. Fixed via a new, narrower exception
subclass and two Driver-level catch sites:

- `app/development_operator/service.py`: added `OperatorAuthorityTransitionError
  (OperatorAuthorizationError)` — reserved specifically for EXPECTED authority transitions
  (lease takeover/expiry, founder cancel racing the effect fence, execution-envelope
  revoke/supersede, supervisor-goal-lease staleness, or another worker concurrently advancing
  the repository/branch/remote). Genuine invariant violations (ambiguous worktree class,
  cross-owner task/job, missing write-identity) stay the base `OperatorAuthorizationError` —
  structural, not just documented, matching this codebase's own doctrine. Every raise site in
  `_require_context()`, `_require_live_execution_authority()`, `_verify_supervisor_goal_
  worktree()`, and `push_branch()` was individually traced and classified into one bucket or
  the other (`WorktreeOwnershipError`, a separate hierarchy, was left untouched — a genuinely
  different, already-intentional design feeding the existing recovery/takeover pipeline).
- `app/development_driver/service.py`'s `run_driver()`: TWO call sites needed the new except
  clause, not one — `_invoke_operator()` (the step's own effect) AND
  `operator.checkpoint_operator_progress()` (the routine post-effect bookkeeping call
  immediately after), discovered empirically because the existing genuine two-connection
  takeover test's mock takes over immediately after step 1's real effect, before the
  bookkeeping checkpoint call runs — a second real call site that would otherwise still crash
  uncaught even with the first fixed. Both now return a `DriverResult` with
  `phase="BLOCKED"`, `classification="STALE_AUTHORITY"`, and a durable checkpoint carrying the
  original reason string, mirroring the existing cancel-path handling in the same loop.
  `state["completed"]` (already-done steps) is preserved as historical either way.

Verified via the existing genuine two-connection takeover test
(`test_operator_lease_effect_time_race.py::test_genuine_cross_session_takeover_mid_run_
blocks_next_step_zero_effect`, updated to assert the new graceful `DriverResult` +
checkpoint instead of `pytest.raises`), plus a negative control (`git stash` on the two
service files: the updated test suite fails to even import on pre-fix code, confirming real
dependency on the fix — not a vacuous assertion). Full `tests/backend/mainai/ -k "operator or
driver"` regression: 27/27 passing.

### Attack criteria for Cursor's own Stage 2 PR, when it appears

1. **Reject sequential setup.** Any new takeover test that mutates `job.locked_by`/
   `lease_generation` on the SAME session immediately before the assertion call is testing
   check-logic, not concurrency — acceptable as a SECOND test alongside a real one, never as
   the only proof.
2. **Require genuine two-connection composition**, matching this session's own new test:
   takeover must commit via a truly separate `Session`, ideally interleaved with an in-flight
   multi-step run (not just "before the very first attempt").
3. **Check for the observability gap named above.** If Cursor's Stage 2 PR adds the
   `OperatorAuthorizationError` → graceful `DriverResult` handling, verify the new
   classification is genuinely distinguishable from cancellation (a founder reading the audit
   trail must be able to tell "superseded by another worker" apart from "founder cancelled"),
   and that the checkpoint is written using the CURRENT (post-takeover) authority state, not a
   stale one.
4. **Recovery after takeover** — CLOSED, empirically proven this session, not just traced:
   `test_operator_lease_effect_time_race.py::test_winning_worker_resumes_from_stale_
   authority_checkpoint_and_completes` runs the LOSING worker to a real `STALE_AUTHORITY`
   result (proving `state["next_step"]` is left unadvanced past the refused step, not
   assumed), then calls `run_driver()` a SECOND time with the WINNING worker's own real
   context (new `worker_id`, the actually-committed bumped `lease_generation`) against the
   SAME task/job/plan. Confirmed: resumes from the exact refused step via `run_driver()`'s
   existing checkpoint-resume design (plan-hash match + stored `driver_state`), completes
   both steps for real, reaches `COMPLETE`, and `completed_steps == 2` (not 3) — zero
   duplication of the already-completed first step. Also traced the Supervisor/Worker layer
   above `run_driver()`: `STALE_AUTHORITY` is deliberately absent from `autonomous_gap.
   service.LIVE_GAP_SIGNAL_CLASSIFICATIONS`, so `development_supervisor/service.py`'s
   post-driver gap-invocation path returns `None` for it (no spurious repair child gets
   created just because authority changed), the task's own `status`/`blocker_reason` are
   left untouched, and `Worker._advance_authorized_supervisor_goals()` only logs the
   classification — the next tick from whichever worker currently holds the lease simply
   re-attempts the same still-`running` task and resumes cleanly, no special-casing needed
   anywhere in that chain.

## Stage 3 — long autonomous soak (8-12 tasks) + report

### The spec already exists, apply it directly

`docs/MAINAI_V1_READINESS.md` Part A (Long-Run Authenticity Spec) is the exact spec for this
stage — ALLOWED/FORBIDDEN lists and the machine-checkable grep-based evidence checklist. Do
not re-derive it; apply it directly to whatever Cursor's Stage 3 PR produces.

### Additions specific to a soak that now includes the gap/repair loop (Stage 1 composed in)

Since Stage 3 explicitly composes Stage 1 (gap/repair) into a longer run, extend the FORBIDDEN
list with gap/repair-specific hidden bridges, precise per this session's own #202 review:

- **Fake repair.** Reject any soak where the repair child's description doesn't match
  `autonomous_gap/service.py`'s own generated format
  (`f"Repair the verification failure blocking: {task.description}"` for a
  `verification_required` gap, or the equivalent capability-missing format) — a mismatch means
  the test hand-inserted a repair task instead of letting `generate_child_task_for_gap()`
  create it. Check via the SAME lookup pattern `#202`'s own `_repair_child()` helper
  established, not a looser string match.
- **Duplicate effects.** With a longer soak (8-12 tasks, multiple gap/repair cycles possible),
  specifically check that a SECOND verification failure on the SAME task never creates a
  SECOND `LifeProblem`/repair child for the identical failure — trace `record_gap()`'s own
  idempotency behavior (not independently re-verified this session; flagged as worth an
  empirical check once a multi-gap soak exists).
- **Stale authority carried across a repair cycle.** The repair child's own narrowed
  `allowed_paths` must be re-derived at REPAIR TIME from the CURRENT scope (confirmed correct
  for #202's single-gap case, via `_narrow_allowed_paths()`) — for a soak with multiple
  founder actions in between (e.g. a scope re-authorization), confirm a LATER repair child
  still narrows from the CURRENT envelope, not a cached one from an earlier point in the run.
- **Fake finalization.** `goal.final_outcome`/`record_final_report()` must only ever be
  reached via the real completion gate, even when the goal's task graph included one or more
  gap/repair detours — the finalization mechanism doesn't change based on how a task reached
  `completed`, but a soak test's OWN assertions should verify this explicitly (check
  `goal.final_outcome` reflects the REPAIR path having happened, e.g. references or is
  consistent with the repair child's own completion, not just "goal completed" as an opaque
  fact).
- **Manual task progression.** Same standing rule as Part A, restated for gap/repair
  specifically: the repair child's OWN status transitions (`ready` → `running` → `completed`)
  must all be real consequences of real Worker ticks, exactly like an ordinary task — #202
  already proves this for the single-gap case; a longer soak must not relax this.

### Machine-checkable additions to Part A's grep-based checklist

```
grep -n "LifeProblem(" <test_file>          # any direct construction (not via record_gap())
                                              # outside the real service module is a red flag
grep -n "InsertedTaskSpec(\|MainAITask(" <test_file>  # a MainAITask() construction whose
                                              # description matches the gap-repair format is a
                                              # hand-insert impersonating a real repair child
```

## Self-improvement package — status

Fully prepared, per `docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md`: concrete goal (SSH-key
marker test-coverage gap, verified real), exact `authorized_paths`/`authorized_capabilities`,
`remote_write_authorized=false`, no provider spend, explicit success criteria, forbidden
actions, and an independent review checklist.

**Gate status**: correction-pass Phases 1-5 are complete (#196/#198/#199/#200/#201, all
merged and independently reviewed). Per `docs/BRANCH_REGISTRY.md`'s own updated tracker, the
founder's stated gate for the long autonomy experiment is satisfied. Per
`docs/ACTIVE_WORK_CURSOR_MAINAI_V1_COMPLETION_RUN.md`'s own stage table, the first bounded
self-improvement run is Stage 4, sequenced after Stage 2 (takeover) and Stage 3 (long soak) —
**do not run it out of that order** without explicit founder direction, even though the
correction-pass gate itself has lifted; Stage 4's own package stays ready and unchanged in the
meantime.

## PR #197 self-review finding — real TOCTOU race in `authorize_execution_scope()`, fixed

Adversarial self-review of this PR's own Path B bridge (5 claims: zero-authority-before-
approval, RLS/ON-CONFLICT safety, `create_plan()` lock deadlock-freedom, concurrent-proposal
canonicality, no doc overclaim) found 4/5 clean and one real, confirmed gap in a DIFFERENT
function this PR also touches:

`authorize_execution_scope()`'s own docstring claimed it "locks the goal row (`SELECT ... FOR
UPDATE`) BEFORE this transition" to serialize against `execute_takeover()`'s matching lock
(the FIRST-GOVERNANCE TOCTOU fence). The code never actually did that select — the only lock
the function took early was on the `ExecutionScopeProposal` row, not `MainAIGoal`; the goal row
was only implicitly, incidentally locked much later, via the `ExecutionAuthorizationEnvelope`
row's own composite FK to `mainai_goals` firing at `db.flush()`. This left a real, narrow
window (during the `prior_envelope` lookup and envelope object construction) where
`execute_takeover()` could acquire the goal lock uncontested, read `EVER_GOVERNED=false`
correctly-at-the-time, and fully dispatch+commit a legacy V0.1 job — immediately followed by
`authorize_execution_scope()` committing the goal's first-ever envelope a moment later. Exactly
the "governance becomes effective mid-flight of an already-decided legacy dispatch" outcome the
docstring calls structurally impossible.

**Why the existing tests didn't catch it**: both existing TOCTOU tests
(`test_first_governance_toctou_race_governance_committed_while_recovery_waits_is_observed` /
`..._recovery_committed_first_then_governance_follows`) manually pre-lock the goal row via raw
SQL `FOR UPDATE` before calling `propose_execution_scope()`/`authorize_execution_scope()` — a
caller-side lock the real router path never takes, so both tests exercised a strictly safer,
fictional code path.

**Fix**: `authorize_execution_scope()` now takes an explicit `SELECT ... FOR UPDATE` on the
goal row immediately after reading the proposal (using `row.goal_id`, the earliest point that
ID is known), held through the rest of the function. Verified no new lock-ordering cycle
(repo-wide grep of every `with_for_update()` call site — `execute_takeover()` is the only other
caller that ever locks `MainAIGoal`, and it never touches `ExecutionScopeProposal`, so this
function's proposal-then-goal ordering can't deadlock against it).

**Empirical proof, not just reasoning**: a new test,
`test_authorize_execution_scope_itself_locks_the_goal_row_no_manual_prelock_needed`, pauses
execution INSIDE `authorize_execution_scope()` (via a `session.add()` interception) at the
exact instant BEFORE the envelope is added — strictly before the incidental FK lock could ever
fire — and attempts a real, concurrent `execute_takeover()` at that precise point. Three-check
verified: passes post-fix (thread B blocks); genuinely FAILS pre-fix via `git stash` (thread B
does NOT block, races ahead, and dispatches a legacy job — the exact forbidden outcome),
confirming this is a real regression guard, not a vacuous assertion. An earlier, simpler
version of this test (pausing only *after* `authorize_execution_scope()` returned) was found
and discarded during this same review pass — it passed identically before AND after the fix,
because the incidental FK lock alone was already sufficient by the time the function returns;
only pausing *inside* the function, before that incidental lock fires, actually isolates the
function's own early-lock behavior.

Also fixed in the same pass: a stale test-name reference in `authorize_execution_scope()`'s own
docstring (cited a test that never existed in the repo, `test_first_governance_toctou_race_
is_serialized_not_racy` — the two real tests have different, longer names).
