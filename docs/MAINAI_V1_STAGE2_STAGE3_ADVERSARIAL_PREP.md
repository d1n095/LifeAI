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

**Real finding, not a security gap**: `run_driver()`'s per-step `try/except`
(`development_driver/service.py:551-553`) only catches `OperatorCapabilityMissing`
gracefully. `OperatorAuthorizationError` from a mid-run takeover propagates *uncaught* out of
`run_driver()`, instead of becoming a clean `DriverResult` (e.g. a `STALE_AUTHORITY`
classification with its own checkpoint) the way cancellation already does in the same loop.
`Worker`'s own per-goal `try/except` in `_advance_authorized_supervisor_goals()` still catches
this at the tick level (confirmed: no crash of the wider poll loop) — but the superseded
task's own audit trail never records a clean "stopped: superseded by takeover" checkpoint the
way a cancel does. **Recommend for Stage 2**: add `OperatorAuthorizationError` to that except
clause, producing a `STALE_AUTHORITY`/`SUPERSEDED` classification + checkpoint, for parity with
the cancel path — small, narrow, non-overlapping with anything Cursor's correction pass
touched.

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
4. **Recovery after takeover**: does the WINNING worker's own subsequent tick correctly pick
   up and continue the task (not get stuck because the LOSING worker's abandoned checkpoint
   confuses readiness/dependency state)? Not yet empirically tested this session — a real gap
   worth an empirical check once Stage 2 lands.

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
