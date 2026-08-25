# Life Supervisor Production Entry (closes the founder-decided execution authority chain)

## What this closes

`docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md` (migrations 0057) built the founder-governed
`execution_scope_proposals -> execution_authorization_envelopes` edge and made it **RUNTIME
REACHABLE** through both founder APIs — a real, active `ExecutionAuthorizationEnvelope` can
exist for a goal. That document explicitly named what was still missing: *"there is still no
durable worker trigger that reconstructs a `SupervisorScope` from an authorized envelope,
derives bounded `WorkBinding`s, and calls `run_supervisor()`... an authorized envelope existing
does not yet cause any autonomous execution to happen."*

This foundation (migration 0059) closes exactly that gap:

```
active ExecutionAuthorizationEnvelope
  -> eligible MainAIGoal                          (app.development_supervisor.production_entry.eligible_authorized_goals)
  -> durable worker trigger                        (app/worker.py's _advance_authorized_supervisor_goals tick)
  -> fenced supervisor_goal_leases claim            (app.development_supervisor.lease, migration 0059)
  -> SupervisorScope reconstructed ONLY from the envelope
  -> narrower per-task WorkBindings
  -> run_supervisor()
  -> Safe Planner / bounded local execution
```

## Authority reconstruction — never invented, never widened

`run_authorized_goal_supervisor_tick()` (`app/development_supervisor/production_entry.py`)
copies every `SupervisorScope` field directly from the goal's CURRENT active
`ExecutionAuthorizationEnvelope` row: `authorized_paths` -> `allowed_paths`,
`authorized_capabilities` -> `allowed_capabilities`, `authorized_risk` -> `maximum_risk`.
Nothing is derived from goal prose, task_type, a `WorkCandidate`'s own content, or a provider
response. `eligible_authorized_goals()` is the ONE place that decides eligibility (a plain
`status = 'active'` read) — a goal with no envelope, a rejected proposal, or a superseded
envelope that was never re-authorized simply never appears there, by construction, not by an
extra check a future caller could forget to add.

`test_retrying_after_the_envelope_was_narrowed_between_ticks_honors_the_new_narrower_scope`
(`tests/backend/mainai/test_supervisor_production_entry.py`) proves the live half of this: a
founder narrowing an already-active envelope mid-flight is honored on the VERY NEXT tick,
because `eligible_authorized_goals()` is re-read fresh every time — never cached, never a
stale reference carried across ticks.

## `supervisor_goal_leases` — the crash/retry/concurrency primitive

`run_supervisor()` is a single BOUNDED call (`SupervisorBounds.max_elapsed_seconds`, default
900s), meant to be invoked repeatedly across many worker ticks for the same goal — not a
one-shot unit of work the existing `mainai_jobs` claim/lease machinery
(`app/jobs/mainai_job_lease.py`) fits. Migration 0059 adds a narrow, dedicated lease table
(`app/development_supervisor/lease.py`), mirroring `AgentScopeLease`'s exact fencing shape
(PR #132's own stale-lease-expiry precedent): `lease_generation` bumped by exactly 1 on every
claim/reclaim, a partial unique index enforcing at most one ACTIVE lease per goal, a single
atomic `INSERT ... ON CONFLICT ... DO UPDATE ... WHERE expires_at < now()` statement that
either creates a fresh lease, takes over a genuinely expired one in place, or is a clean no-op
if another worker still legitimately holds it.

Covered explicitly (`tests/backend/mainai/test_supervisor_goal_lease.py`,
`test_supervisor_production_entry.py`): two workers racing the same goal (exactly one wins), a
crashed worker's lease blocking recovery only until it genuinely expires (never before, never
by force), release making a goal immediately reclaimable rather than waiting out the full TTL,
and a worker resuming its OWN prior `mainai_jobs` claim across two separate ticks for the same
goal (see "job-lease resume" below) without ever needing to re-claim it.

## The other lease: fencing against V0.1's own separate execution path

`app/mainai_execution/execution_job.py`'s `run_task_execution_job()` (MainAI Execution Loop
V0.1/V0.2) is a COMPLETELY SEPARATE, pre-existing execution path — `app/worker.py`'s own
`_advance_mainai_execution_tasks()` tick has ALWAYS unconditionally dispatched every `ready`
task whose `task_type` the goal's `approval_policy` marks AUTO (which `standard_repo_work` —
the default policy, and what `WorkCandidate` authorization sets — does for `repo_edit`,
`run_tests`, `open_pr`, and `read_only_audit`), with **zero envelope awareness at all**. This
predates the whole envelope/Supervisor foundation and was never part of it.

Two consequences, both handled here:

1. **`_advance_mainai_execution_tasks()` now EXCLUDES any task whose goal has an active
   `ExecutionAuthorizationEnvelope`** (`app/worker.py`). Without this, the entire envelope
   system would be decorative: V0.1's blind tick would keep auto-dispatching
   envelope-governed tasks regardless of `allowed_paths`/`allowed_capabilities`/
   `maximum_risk`, usually winning the race simply by running earlier in the same poll cycle
   (see `run_once()`'s own tick ordering). Goals with no envelope are completely unaffected —
   see `tests/backend/test_advance_tasks_excludes_envelope_governed_goals.py`.
2. **`prepare_context()`'s own `mainai_jobs` lease claim** (a SEPARATE lease from
   `supervisor_goal_leases`, one per dispatched task/job, reusing
   `app/jobs/mainai_job_lease.py`'s existing fencing primitive via a new
   `claim_specific_mainai_job()`) closes the remaining race: even with exclusion #1 in place,
   nothing else should be allowed to pick up the exact job `dispatch_ready_task()` just
   created inside this same Supervisor call. A job still `queued` is claimed atomically; a job
   already `running` is treated as a legitimate RESUME of this same goal's own earlier
   Supervisor session (safe specifically because `supervisor_goal_leases` already guarantees
   at most one worker runs `run_supervisor()` for a given goal at a time, and exclusion #1
   above removes the only other source of a competing claim) — never re-claimed, never a hard
   failure for the expected two-call resume case `run_supervisor()`'s own design already
   relies on (`test_two_job_chain_and_interruption_resume_are_canonical`).

## Local-only worktree, keyed by goal — not by job

`SupervisorScope.repository_identity` is a single fixed string for an ENTIRE
`run_supervisor()` call, checked for exact equality against every dispatched task's own
`OperatorContext.repository_root.resolve()`. Since `run_supervisor()` can dispatch several
different tasks within one bounded call, and a job's id does not exist until
`dispatch_ready_task()` creates it (after the scope is already built), a per-JOB worktree
directory (the model `app/mainai_execution/worktree.py` already uses for V0.1) cannot satisfy
that fixed, pre-declared identity — see `app/development_supervisor/production_worktree.py`'s
own module docstring for the full reasoning. This foundation instead uses a real, isolated
`git worktree add` keyed by `goal.id` alone (fully deterministic before `run_supervisor()` is
even called), reused across every task attempted for that goal, including across separate
ticks.

**Deliberately LOCAL ONLY, on purpose:** this worktree is created off the worker process's own
on-disk checkout, never fetched from or pushed to GitHub. `OperatorContext.
remote_write_authorized` stays at its default `False` throughout, and `push_branch` (the one
`REMOTE_WRITE` capability in `DEVELOPMENT_CAPABILITIES`) is never something this production
entry grants regardless of what an envelope authorizes. Real remote pushes remain a separate,
NOT-YET-authorized capability — expanding into them is a distinct, later founder act, matching
the founder decision's own staging. `app/mainai_execution/worktree.py`'s existing job-scoped,
GitHub-push-capable model (used by V0.1) is completely untouched by this change except for one
internal refactor: `create_task_worktree()` was split into a synchronous core
(`create_task_worktree_sync()`) plus a thin async wrapper, purely so a caller in a sync
context (`WorkBinding.prepare_context` is a plain, never-awaited callable) can still use it —
existing callers and behavior are unchanged.

## Proof level: RUNTIME REACHABLE, still not autonomous repo-writing

Every real task this wiring reaches, without a hand-built `PlanCandidate` or a gap-derived
deterministic repair recipe, legitimately defers as `PROVIDER_SPEND_NOT_AUTHORIZED` — because
`scope.provider_spend_authorized` is hardcoded `False` here (a bare authorized envelope never
implies provider spend, per `SupervisorScope`'s own docstring, itself a founder P1 review
finding predating this work). This is an honest, safe, fully testable **RUNTIME REACHABLE**
outcome for the trigger itself, not a bug and not evidence of autonomous execution.
`tests/backend/mainai/test_supervisor_production_entry.py`'s own tests prove real
`run_supervisor()` invocation (a real `MainAICheckpoint` row written, a real `mainai_jobs` row
created and leased, a real local git worktree materialized) without claiming
**PRODUCTION E2E PROVEN** for repo-writing autonomy — that would require a further, separate
founder act to authorize provider spend and/or remote write, exactly the layered-authority
design this whole mission has held to throughout.

## Known follow-up (not fixed here, out of this PR's scope)

**A pre-existing composite-FK defect in already-merged migration 0057.** While writing this
PR's own regression test for `supervisor_goal_leases.envelope_id`'s `ON DELETE SET NULL`
(see "Local-only worktree" section above for the analogous fix this PR makes to its OWN new
table), the SAME defect was found in migration 0057's `execution_scope_proposals.
authorized_envelope_id` composite FK: a bare `ON DELETE SET NULL` (no column list) tells
PostgreSQL to null EVERY referencing column on delete, including `owner_id` — which is
`NOT NULL` — so deleting a referenced envelope while a proposal still points at it via
`authorized_envelope_id` raises a constraint violation instead of cleanly detaching.

**Confirmed NOT currently reachable through real account erasure**, however:
`erase_own_execution_authorization_children()` (migration 0057's own function) deletes
`execution_scope_proposals` in a separate, EARLIER statement than `execution_authorization_
envelopes` — by the time envelopes are deleted, no proposal row references them anymore, so
the defect never actually fires in that flow (verified directly against Postgres with the
exact two-statement ordering the function uses). The self-referential `supersedes_envelope_id`
FK has the same bare-SET-NULL shape but is also unreachable in practice: erasure deletes an
owner's superseded-and-superseding envelope rows in ONE batch `DELETE`, and Postgres's FK
trigger has nothing left to null by the time it would fire. This is a latent DDL defect (bad
practice, a landmine for any FUTURE code path that deletes an envelope without first clearing
every referencing row) — not an active production bug today. Still worth fixing properly (the
column-specific `ON DELETE SET NULL (authorized_envelope_id)` / `(supersedes_envelope_id)`
form this PR's own migration 0058 already uses) in its own small, separate fix-forward PR,
matching this project's own "don't blend unrelated fixes into an unrelated branch" discipline
— reserved as a near-term priority, not fixed here.

`app/work_candidates/service.py`'s `_propose_execution_scope_if_actionable()` (PR #144)
proposes capability names from a coarse, WorkCandidate-level vocabulary
(`"repo_read"`/`"repo_edit"`/`"run_tests"`), while `run_supervisor()`'s own `validate_scope()`
checks `scope.allowed_capabilities` against `app.development_operator.service.
DEVELOPMENT_CAPABILITIES`'s granular operator-level vocabulary (`"read_file"`,
`"patch_file"`, `"run_focused_test"`, `"stage_scoped_changes"`, ...) — the two vocabularies do
not overlap at all today. A founder authorizing a WorkCandidate-derived proposal exactly as
proposed would therefore produce an envelope `run_supervisor()`'s own `validate_scope()`
rejects outright (`"capability envelope contains missing or unsafe capability"`). This is a
real, separate gap (a vocabulary/translation layer is missing, likely in
`authorize_execution_scope()` or in the proposal step itself) — reserved for its own follow-up
PR per this project's own "don't blend unrelated fixes into an unrelated branch" discipline
(`CLAUDE.md`), not fixed here. This PR's own tests construct envelopes with
`DEVELOPMENT_CAPABILITIES`-vocabulary strings directly to stay honest about what
`run_supervisor()` itself actually requires.

## Test coverage

- `tests/backend/mainai/test_supervisor_goal_lease.py` (7): claim/renew/release/takeover
  fencing for `supervisor_goal_leases`, including the two-workers-race and
  crash-then-genuine-expiry-reclaim scenarios.
- `tests/backend/test_supervisor_production_worktree.py` (3): a real local `git worktree add`,
  idempotent goal-scoped reuse (reporting the CURRENT head after intervening local commits),
  and independent isolation between two different goals' worktrees.
- `tests/backend/mainai/test_supervisor_production_entry.py` (12): eligibility (active
  envelope + running goal required, superseded envelope revokes eligibility, blocked/waiting
  goals excluded even with an active envelope), authority reconstruction (scope copied only
  from the envelope, a task exceeding the authorized risk ceiling never dispatched), the full
  concurrency/crash attack list from the founder decision's own section 10 (lease already
  held, two workers racing, a crashed worker's lease reclaimed only after genuine expiry,
  re-authorization narrowing honored immediately on the next tick), and the clean no-op case
  (no bindable task).
- `tests/backend/test_advance_tasks_excludes_envelope_governed_goals.py` (2): V0.1's own
  blanket auto-dispatch tick correctly excludes envelope-governed goals' tasks while leaving
  ordinary (non-autonomous) goals completely unaffected.

## Explicitly deferred

- No provider-spend or remote-write authorization path — see "Proof level" above; a separate,
  later founder act, not invented here.
- No capability-vocabulary translation between `WorkCandidate` proposals and
  `DEVELOPMENT_CAPABILITIES` — see "Known follow-up" above.
- No frontend UI — matching every other foundation in this mission.
- No `docs/BRANCH_REGISTRY.md` update in this PR — a Cursor PR was actively editing that exact
  file when this branch was pushed; the registry entry follows in a small, separate PR once
  that work lands, matching the same coordination discipline PR #144 already established.
