# MainAI Execution Loop V0.1

Branch: `claude/mainai-execution-loop-v0-1`. This document describes the IMPLEMENTED reality of
V0.1 as of this branch's head — not the aspiration, not what a later version will add. Where
something is stubbed, limited, or simply not built, it is named as such below rather than
implied to work.

V0.1's shape: **Goal → Plan → Task Graph → Executor (via the existing `mainai_jobs` runtime) →
Checkpoint/Verify → Approval Gate → Final Report**, plus an **Engineering Lesson / safety
memory** foundation that can influence planning. No new job queue, lease, or heartbeat system
was built — every execution attempt is still a real, leased, fenced `mainai_jobs` row
(migration 0025-0029); this branch only adds a durable Goal/Plan/Task graph *above* that
existing runtime and a worker tick that keeps it moving.

## Architecture at a glance

```
MainAIGoal (1) ──< MainAIPlan (versioned, one active) ──< MainAITask (graph via
MainAITaskDependency) ──dispatch──> mainai_jobs row (job_type="task_execution") ──worker
claims/executes──> app/mainai_execution/execution_job.py ──> checkpoint / verify / finalize
──> MainAITaskEvent (append-only) + MainAICheckpoint (append-only)
```

Every table (`mainai_goals`, `mainai_plans`, `mainai_tasks`, `mainai_task_dependencies`,
`mainai_task_events`, `mainai_checkpoints`, `engineering_lessons`) was added in migration 0032.
See `app/models/mainai_execution.py` for the full column-level rationale.

## Module map

| Module | Responsibility |
|---|---|
| `app/mainai_execution/planner.py` | `create_goal()`, `create_plan()` (deterministic persist + validation + cycle rejection + replan supersession), `propose_plan_via_ai()` (the one non-deterministic step) |
| `app/mainai_execution/graph.py` | `recompute_task_readiness()`, `next_ready_task()` |
| `app/mainai_execution/executor.py` | `dispatch_ready_task()` (approval gate + real `mainai_jobs` row creation), `task_for_job()`, `retry_task()`, `cancel_task()` |
| `app/mainai_execution/approval.py` | `APPROVAL_POLICIES`, `require_task_approval()` (the enforcement point), `grant_task_approval()` |
| `app/mainai_execution/verify.py` | `verify_task()` — runs a task's closed-vocabulary `verification_plan` |
| `app/mainai_execution/checkpoint.py` | `record_checkpoint()` / `latest_checkpoint_for_step()` |
| `app/mainai_execution/liveness.py` | `task_liveness()` — pure read-only classification |
| `app/mainai_execution/final_report.py` | `generate_goal_report()` / `record_final_report()` |
| `app/mainai_execution/lessons.py` | `record_lesson()`, `lookup_lessons()`, `apply_lessons_to_verification_plan()` |
| `app/mainai_execution/execution_job.py` | `run_task_execution_job()` — the actual executor, dispatched from `app/worker.py` |
| `app/worker.py` (`_advance_mainai_execution_tasks`) | The auto-advance tick that makes the loop self-propelling |
| `app/routers/mainai_execution.py` | The minimal founder-only API |
| `frontend/app/(shell)/admin/mainai-execution/page.tsx` | The minimal founder UI |

## REAL

- Structured, versioned planning with deterministic validation (unknown `task_type`,
  out-of-range `depends_on`, dependency cycles via 3-color DFS) — a bad plan never partially
  lands.
- AI-assisted plan proposal (`propose_plan_via_ai()`) is strictly JSON-parsed and never trusted
  as free text; it feeds the same deterministic `create_plan()` a human-authored plan would.
- Real dependency graph with readiness promotion and failure-propagation to `blocked`.
- Real dispatch through the existing `mainai_jobs` runtime — a `task_execution` job is leased,
  fenced (`lease_generation`), heartbeated, and reclaimed by the exact same code
  (`app/jobs/mainai_job_lease.py`) every other job type already uses.
- Real approval gate enforced in code: `require_task_approval()` runs inside
  `dispatch_ready_task()` **before** any `mainai_jobs` row exists. An unapproved task cannot
  reach execution — not "shouldn't", cannot.
- Real verification, separate from execution: `verify_task()`'s boolean result is the only
  thing that gates `completed`. A handler's own "I'm done" never sets task status.
- Real checkpoint/resume: a work-in-progress AI result and (once `github_write_enabled` is on)
  a completed GitHub push are both checkpointed *before* the step that could crash next, so a
  reclaimed job resumes from durable state, never repeats a non-idempotent step.
- Real autonomous advance: `app/worker.py`'s `_advance_mainai_execution_tasks()` dispatches
  every `ready` task, every poll cycle, across all owners — the loop does not need an external
  caller to keep moving once a plan exists.
- Real GitHub multi-file commit capability (`app/integrations/github_client.py`'s
  `commit_multiple_files()`, Git Data API, fast-forward-only ref update) — not a stub. Gated
  behind `settings.github_write_enabled` (default `False`); see LIMITED below for exactly what
  that means today.
- Real, structured, durable final report (`generate_goal_report()`), assembled purely from
  durable rows — never an LLM call — keeping execution-attempt status, task outcome,
  verification outcome, and approval state as separate fields, never collapsed into one.
- Real engineering-lesson provenance and real influence on planning:
  `apply_lessons_to_verification_plan()` can add a missing regression test to a new task's
  `verification_plan`, with the lesson's id recorded on that task's own `created` event.
- Real task cancellation for anything not currently running
  (`pending`/`ready`/`blocked`/`retryable_failed`).
- Real minimal API and UI, both founder-only, both reading/acting on the real state above.

## STUBBED

- Nothing in this list. Every piece named above does real work against real state. The one
  capability that was previously a stub — the GitHub commit — is now real (see REAL above); see
  LIMITED for the one thing that still doesn't happen by default (a live network push).

## LIMITED

Named explicitly rather than silently left implicit:

1. **`github_write_enabled` stays at its documented default (`False`) in this environment.**
   `repo_edit`/`open_pr` still do everything up to and including real local file writes and
   real local `pytest` verification; with the flag off they produce a durable, evidence-backed
   PROPOSAL (computed branch name, file contents, commit message / PR title+body) rather than a
   live GitHub push. This matches `app/agent_orchestration.py`'s own established
   propose-only-by-default convention and was never flipped on for V0.1 — doing so is a
   separate, explicit decision (a real external write to GitHub), not something this branch
   changed unilaterally.
2. **Cancelling a `running` task_execution job does not stop it mid-flight.**
   `executor.cancel_task()` only accepts `pending`/`ready`/`blocked`/`retryable_failed`. A
   `running` task has one real `mainai_jobs` row already in flight — a single AI call plus a
   single verification pass, with no natural safe mid-task checkpoint to cooperatively check a
   cancel flag at (unlike `corpus_review`'s per-document loop). `service.request_cancel()` still
   works on the underlying job (`cancel_requested` is set and visible), but
   `execution_job.py` does not check it mid-task — a running attempt runs to its natural
   completion or failure regardless.
3. **No task_type ever transitions a task into `waiting_external`/`waiting_ci`.** Both statuses
   exist in the closed vocabulary and `task_liveness()` correctly never misclassifies them as
   `stalled`/`dead` — but V0.1 ships exactly four task types
   (`read_only_audit`/`repo_edit`/`run_tests`/`open_pr`), none of which produces a genuine
   external/CI wait. "Read CI, stop before merge" from the original spec is therefore not
   exercised live: there is no CI-polling task type built.
4. **A retried task (`retry_task()`) is not automatically re-dispatched by that call.** It
   relies on the next relevant auto-advance tick (worker poll cycle, or another task in the
   same goal completing) to pick it back up — in practice near-immediate, but not synchronous
   with the retry call itself.
5. **Replan is a full fresh breakdown, not a surgical patch.** `create_plan()` called again for
   a goal with an active plan supersedes it wholesale and cancels its still-unstarted tasks;
   already-completed tasks and all history are untouched, but there is no partial/merge replan.
6. **A goal only closes (`completed`/`failed`, `final_outcome` populated) when its report is
   read** (`GET /goals/{id}/report` calls `record_final_report()`). There is no separate
   background process that closes a goal the instant its last task finishes — this is a
   deliberately simple V0.1 choice, not an oversight.
7. **The minimal API/UI has no plan-editing surface.** A plan is created wholesale
   (AI-proposed) via `POST /goals/{id}/plan`; there is no endpoint to edit an individual
   task's fields after creation, only approve/reject/cancel/retry.

## NOT IMPLEMENTED

- Dead-agent takeover / salvage (a second worker picking up and *productively completing*
  work an abandoned worker left genuinely mid-step, beyond the existing lease-reclaim +
  checkpoint-resume already built) — see V0.2 CANDIDATES.
- Any CI-integration task type.
- Multi-tenant approval routing (today's `standard_repo_work` policy is the only one shipped;
  there is no UI for defining a new named policy).
- Automatic engineering-lesson extraction from a completed goal's own outcome (lessons are
  recorded by a human/process calling `record_lesson()` today, never self-generated by MainAI
  from its own run history).

## KNOWN RISKS

- **`_advance_mainai_execution_tasks()` scans every `ready` task across all owners on every
  poll cycle.** Fine at V0.1's scale; a large number of concurrently-ready tasks across many
  goals would make this scan (and its per-task `dispatch_ready_task()` calls, each its own
  commit) the dominant cost of a poll cycle. No pagination/limit exists yet.
- **A `running` task_execution job cannot be cooperatively cancelled** (see LIMITED #2) — a
  founder who cancels a long-running or stuck task must wait for it to reach a terminal state
  on its own; there is no kill switch beyond the underlying `mainai_jobs` lease eventually
  expiring if the worker itself dies.
- **`engineering_lessons` has no lesson-vs-lesson conflict detection** (unlike
  `KnowledgeClaim`'s `assess_claim_confidence()`) — two contradictory lessons can both sit
  `active` with nothing flagging the contradiction. Documented explicitly in
  `EngineeringLessonConfidence`'s own docstring.
- **`lookup_lessons()` returns every active lesson matching `applies_to_any`, uncapped.**
  Hardening pass, performance/bounds review: the query itself is GIN-indexed (migration 0032's
  `ix_engineering_lessons_applies_to`), so it's not a full scan, but there is no `LIMIT` on the
  result set. Not practically relevant at V0.1's scale — lessons are created deliberately by
  hardening passes, not at per-task-execution volume — but would need one if the lesson corpus
  ever grew into the thousands.
- **Captured subprocess stdout/stderr (`stdout_tail`/`stderr_tail` on `run_tests`/verification
  evidence) is not secret-scanned or redacted before being stored as durable
  `MainAITaskEvent.detail`.** If a test file (including one an AI-authored `repo_edit` just
  wrote) prints an environment variable or other sensitive value, that value is captured
  verbatim (truncated to the last 4000/2000 chars) and persisted. Mitigated, not eliminated, by
  the same owner-scoped RLS every other execution-loop table already has — only the task's own
  owner (or the founder) can read it — but there is no scrubbing of the content itself.

## SECURITY INVARIANTS

- Every `mainai_*` execution table is owner-scoped RLS (`FORCE ROW LEVEL SECURITY`), same
  convention as `mainai_jobs` — except `engineering_lessons`, which is deliberately NOT
  owner-scoped (see MEMORY / ENGINEERING LESSON MODEL below).
- `mainai_task_events` and `mainai_checkpoints` are append-only at the database level
  (`BEFORE UPDATE OR DELETE` triggers deny mutation unconditionally for UPDATE, and deny DELETE
  unless an authorized owner-erasure GUC flag is set) — the same pattern `mainai_job_events`
  already established.
- Composite owner foreign keys (`UNIQUE(id, owner_id)` on parents + matching composite FK on
  children) prevent the cross-owner row-injection class of bug the mainai_jobs correction round
  (migration 0026/0027) originally closed.
- Every write path re-validated: `app/rls.py`'s `apply_mainai_execution_privileges()` narrows
  `mainai_app`'s grants down to exactly the privileges each table needs (mirrors
  `apply_mainai_job_runtime_privileges()` exactly, including the advisory-lock-guarded
  enforce-then-verify-against-catalogs shape).
- The API is founder-only (`require_founder`) with the exact same dependency every other
  founder router already uses; owner isolation for goal/task rows is enforced by Postgres RLS
  through the ordinary session, never re-derived in application code.
- **Every AI-proposed path is validated before it can reach a filesystem write or a subprocess
  argv** — never trusted just because the plan/response parsed as well-formed JSON: a
  `targeted_tests` verification target is rejected if absolute or containing `..`
  (`validate_targeted_tests_target()`, enforced at plan-creation time AND at both real execution
  call sites), and a `repo_edit` task's code-agent-proposed file path is rejected the same way
  (`_validate_repo_edit_file_path()`), with a second, independent `resolve()`-and-compare
  confinement check in `_handle_repo_edit()` itself as defense in depth against a symlink
  already present in the checked-out tree. Hardening-pass finding: the file-write path was, for
  a time, missing the absolute-path half of this check — an AI-proposed absolute path was a
  genuine arbitrary-file-write primitive onto the executor host, since `Path("/repo/root") /
  "/etc/x"` evaluates to `Path("/etc/x")`, not an error (pathlib's own documented `/` semantics
  for an absolute right-hand operand). Fixed; see `docs/BRANCH_REGISTRY.md`.

## DURABILITY INVARIANTS

- A task can never be `completed`/`failed`/`cancelled` with a NULL `completed_at`
  (DB CHECK constraint `ck_mainai_tasks_completed_at_matches_terminal_status`) — this caught a
  real test bug during development and is exactly the invariant it was designed to catch.
- A task's `verification_plan` and every event's `detail` are structured JSON, never free text
  the executor could reinterpret.
- Checkpoints are scoped to the exact `(task_id, job_id, step)` triple — a checkpoint from a
  PREVIOUS attempt (an earlier `mainai_jobs` row for the same task, e.g. after a real retry) is
  never reused as if it were the current attempt's own state.
- `mainai_job_id` on a task points at the real, leased, heartbeated job currently or most
  recently executing it — liveness/retry/cancellation of the actual work is governed entirely
  by that already-reviewed runtime.

## APPROVAL MODEL

- `APPROVAL_POLICIES` is a NAMED registry (`MainAIGoal.approval_policy` points at one by name),
  never inline logic scattered across call sites. V0.1 ships exactly one policy,
  `standard_repo_work`, which marks all four known task types AUTO by default.
- A per-task `approval_required=True` flag ALWAYS overrides the policy default — set by the
  planner (or a founder editing a plan) for any individual task regardless of its `task_type`.
- An unknown policy name or task_type fails CLOSED (requires approval) rather than guessing
  AUTO.
- The gate is enforced inside `dispatch_ready_task()`, before `create_job()` — there is no
  code path, including the worker's own auto-advance tick, that can create a `mainai_jobs` row
  for an unapproved task. Proven by
  `test_dispatch_ready_task_stops_an_approval_required_task_with_no_grant_recorded`.

## VERIFICATION MODEL

- `verify_task()` runs every entry in a task's `verification_plan` in order; ALL steps must
  pass for the overall result to pass. Currently one step kind ships: `targeted_tests` (real
  subprocess `pytest`). An unknown step kind raises `VerificationStepError`, never silently
  skipped and called "passed".
- For `run_tests` tasks specifically, the task's own real pytest run IS its verification
  (`execution_job.py` derives the `VerificationResult` directly from that run's outcomes,
  rather than independently re-running the same targets a second time via `verify_task()`).
- `_finalize_task_outcome()` is the ONLY place a task becomes `completed` — gated strictly on
  `verification.passed`. A failed verification produces `retryable_failed` (attempts remain) or
  `failed` (exhausted), never `completed`. Proven end-to-end in Demo 2.

## MEMORY / ENGINEERING LESSON MODEL

- `engineering_lessons` is its own table, deliberately NOT a `KnowledgeClaim` row and
  deliberately NOT RLS-protected — founder-wide project/system knowledge, never mixed with any
  individual user's private memory. No `owner_id` column exists on it at all (verified by a
  real test: `test_demo_4_...` checks both the absence of the column and that
  `pg_class.relrowsecurity` is `false` for the table).
- Every lesson requires `source_type`/`source_ref` at the model level (NOT NULL) — there is no
  code path in `lessons.py` that can create an unsourced lesson.
- `lookup_lessons()` uses the GIN-indexed jsonb `?|` "any array element matches" operator
  against `applies_to` tags.
- `apply_lessons_to_verification_plan()` is the one place a lesson actually DOES something —
  wired into `planner.create_plan()`, never into the executor (a lesson influences what gets
  PLANNED, a durable, reviewable decision, not what an already-dispatched task does on the fly).
  It only ever ADDS a missing `targeted_tests` step, never removes one the planner/founder
  specified.

## RESTART / RESUME

- A worker crash mid-task (the process dying with an uncaught exception, or genuinely being
  killed) leaves the `mainai_jobs` row `running` with an expiring lease and a durable
  `work_result` checkpoint already committed.
- A second worker reclaims through the exact, already-reviewed lease-expiry mechanism
  (`claim_next_mainai_job()`) — no special "resume" API exists or is needed.
- `run_task_execution_job()` checks for an existing checkpoint for the current `job_id`/step
  before repeating the AI call (or, once `github_write_enabled` is on, the GitHub push) —
  proven with a real call-count assertion: the LLM provider is invoked exactly once total
  across two separate `run_task_execution_job()` invocations, and exactly one `completed`
  event is ever recorded despite the reclaim.
- Verification itself is NOT checkpointed (it is cheap, local, and safely re-runnable) —
  proven correctly re-running on resume via `verify_calls["n"] == 2` in
  `test_mainai_execution_resilience.py`/`test_mainai_execution_demos.py`.

## DEMO RESULTS

All four demos are real, automated tests (not manual walkthroughs), run through the actual
production code paths described above — see `tests/backend/test_mainai_execution_demos.py`.

1. **Success (Demo 1):** `test_demo_1_full_success_path_end_to_end_through_the_real_worker_poll_loop`
   — a goal ("Gör en read-only repo audit, hitta en mycket liten behavior-neutral
   dokumentations- eller testreferensfix, genomför den, verifiera den och öppna en PR"),
   AI-proposed and persisted via the real planner, driven to completion purely by repeated
   `Worker().run_once()` calls — zero manual dispatch from the test. All four tasks
   (`read_only_audit`→`repo_edit`→`run_tests`→`open_pr`) reach `completed` in dependency
   order; the local file write and its `pytest` verification are real; `open_pr` produces a
   real, evidence-backed PR proposal (no live network call, per LIMITED #1); the final report
   shows zero unresolved risk. **PASSED.**
2. **Failure (Demo 2):** `test_demo_2_failure_path_never_falsely_completes_and_the_report_tells_the_truth`
   — a deliberately failing `run_tests` task lands on `retryable_failed`, never `completed`;
   its dependent is never advanced (stays `pending`, no `dispatched` event); the report shows
   `verification_outcome.passed == False` and `unresolved_risk == True`. **PASSED.**
3. **Restart/resume (Demo 3):** `test_demo_3_restart_resume_through_the_real_worker_poll_loop`
   — dispatch and the real reclaim/resume both run through unmodified production code
   (including `Worker().run_once()` for the resume half); the simulated crash itself bypasses
   `process_claimed_mainai_job()`'s own broad exception handler (which correctly turns a
   genuine application bug into a truthful `mark_failed()` outcome — not equivalent to a real
   process crash that never gives Python's `try/except` a chance to run at all). AI provider
   called exactly once total; exactly one `completed` event despite two execution attempts.
   **PASSED.**
4. **Engineering lesson (Demo 4):**
   `test_demo_4_a_real_historical_lesson_influences_planning_and_stays_separate_from_user_private_memory`
   — a REAL historical incident from this project's own history (PR #36's founder re-review
   round, "BLOCKER: implement real lease fencing for `mainai_jobs`") recorded with full
   provenance, found by tag lookup, shown to actually augment a new `repo_edit` task's
   `verification_plan` via the real planner (with the lesson's id on that task's own `created`
   event), and shown structurally separate from user-private memory (no `owner_id` column, RLS
   not enabled on the table). **PASSED.**

## V0.2 CANDIDATES

Documented, not built, per explicit instruction:

- **Dead-agent takeover / salvage / resume hardening.** Today's resume story is "the SAME
  work resumes once reclaimed" — real and tested, but assumes the reclaiming worker runs the
  identical code. V0.2 should address: a worker that reclaims a job whose original worker is
  provably dead (not just lease-expired) taking deliberate salvage action; partial-progress
  salvage for a task type with real intermediate steps; and explicit dead-worker detection
  distinct from ordinary lease expiry (today `stalled` and `dead` are both liveness
  *classifications*, not states anything acts on automatically).
- **A CI-integration task type**, closing LIMITED #3 (`waiting_ci` genuinely reachable).
- **Cooperative cancellation for a `running` task_execution job**, closing LIMITED #2 — likely
  requires task types with real intermediate steps to check a cancel flag at, which V0.1's
  current four task types mostly don't have.
- **Lesson-vs-lesson conflict detection**, mirroring `KnowledgeClaim`'s
  `assess_claim_confidence()` (closing the KNOWN RISK above).
- **Bounded/paginated auto-advance scan** if the number of concurrently-ready tasks across
  owners grows large enough for it to matter.
- **Automatic re-dispatch on `retry_task()`** instead of relying on the next natural
  auto-advance trigger (LIMITED #4).

## Coverage matrix

Every item from the founder's required test list, mapped to where it is actually covered. An
item not covered by a real test is named as a gap, not silently assumed.

| Requirement | Covered by |
|---|---|
| Structured planning | `test_mainai_execution_planner.py` (section B) |
| Deterministic validation | `test_mainai_execution_planner.py` (section C) |
| Plan versioning | `test_create_plan_called_again_supersedes_previous_plan_and_cancels_its_unstarted_tasks` |
| Dependency graph | `test_mainai_execution_planner.py` (section E), `graph.py` |
| Cycle rejection | `test_create_plan_rejects_a_dependency_cycle_and_writes_nothing`, `test_detect_cycle_pure_function_finds_and_clears_cycles` |
| Readiness | `test_recompute_task_readiness_promotes_dependents_once_their_dependency_completes`, `..._is_idempotent` |
| Failed dependency handling | `test_recompute_task_readiness_blocks_a_dependent_of_a_failed_task_rather_than_stalling_silently` |
| Approval gate | `test_dispatch_ready_task_succeeds_once_approval_is_explicitly_granted` |
| Approval bypass rejection | `test_dispatch_ready_task_stops_an_approval_required_task_with_no_grant_recorded` |
| Task dispatch | `test_dispatch_ready_task_needs_no_approval_for_a_task_the_policy_marks_auto`, Demo 1 |
| `mainai_jobs` integration | all `test_mainai_execution_executor.py` end-to-end tests (real `create_job`/`claim_next_mainai_job`) |
| Lease | reused directly — every dispatch creates a real leased row; Demo 3 exercises reclaim |
| Fencing | reused directly from `test_mainai_jobs.py` (`_guarded_job_write` applies unchanged to `task_execution` jobs) |
| Duplicate worker protection | `test_two_workers_racing_many_jobs_never_claim_the_same_job` (`test_mainai_jobs.py`, job_type-agnostic) |
| Checkpoint persistence | `test_record_and_lookup_checkpoint_round_trip`, `..._is_scoped_to_the_exact_job_id`, `..._distinguishes_steps` |
| Resume | `test_run_task_execution_job_resumes_from_checkpoint_...`, Demo 3 |
| Idempotent retry | `test_retry_task_moves_a_retryable_failed_task_back_to_ready`, `..._rejects_any_non_retryable_status` (parametrized) |
| Heartbeat/liveness | `test_mainai_execution_resilience.py` section C (`task_liveness` running/stalled/dead) |
| `waiting_external` | `test_task_liveness_never_misclassifies_waiting_external_as_stalled_or_dead` |
| `waiting_ci` | `test_task_liveness_never_misclassifies_waiting_ci_as_stalled_or_dead` |
| Stalled | `test_task_liveness_stalled_when_lease_expired_but_not_yet_reclaimed` |
| Dead | `test_task_liveness_dead_when_the_linked_job_is_missing`, `..._already_reached_a_terminal_state` |
| Verification success | `test_verify_task_passes_when_the_targeted_test_passes`, Demo 1 |
| Verification failure | `test_verify_task_fails_when_the_targeted_test_fails`, Demo 2 |
| No false completion | `test_run_task_execution_job_verification_failure_never_completes_the_task`, `..._run_tests_derives_verification_from_its_own_work_result...`, Demo 2 |
| Cancellation | `test_cancel_task_cancels_a_not_yet_running_task_and_blocks_its_dependents` (parametrized), `..._rejects_a_running_task`, `..._rejects_an_already_terminal_task` |
| Retry | see "Idempotent retry" above |
| CI waiting | **NOT COVERED — no task_type reaches this state in V0.1** (see LIMITED #3, NOT IMPLEMENTED) |
| CI failure | **NOT COVERED — same reason** |
| Final report truthfulness | `test_generate_goal_report_keeps_outcome_verification_and_approval_separate`, `test_record_final_report_...` (×2), Demos 1/2 |
| Engineering lesson provenance | `test_record_and_lookup_lessons_by_tag`, Demo 4 |
| Lesson lookup/application | `test_apply_lessons_to_verification_plan_*` (3 tests), `test_create_plan_is_actually_influenced_by_a_real_previously_recorded_lesson`, Demo 4 |
| Durable state after restart | Demo 3, `test_mainai_execution_resilience.py` section B |

## Test suite

New test files added by this branch:

- `tests/backend/test_github_client.py`
- `tests/backend/test_mainai_execution_planner.py`
- `tests/backend/test_mainai_execution_executor.py`
- `tests/backend/test_mainai_execution_resilience.py`
- `tests/backend/test_mainai_execution_demos.py`
- `tests/backend/test_mainai_execution_api.py`

Run the full backend suite: `pytest tests/backend tests/security tests/account`. See the final
PR description for the exact pass/fail counts from the last full run before this branch's PR
was opened.
