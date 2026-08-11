# MainAI Long-Running Orchestration V0.3

Branch: `claude/mainai-long-running-orchestration-v0-3`. This document describes the IMPLEMENTED
reality of V0.3 as of this branch's head — not the aspiration, not what a later version will
add. Where something is stubbed, limited, or simply not built, it is named as such below rather
than implied to work. Written in the same discipline as
`backend/docs/MAINAI_EXECUTION_LOOP_V0_1.md` and `backend/docs/MAINAI_DEAD_AGENT_RECOVERY_V0_2.md`,
which this branch builds directly on top of — V0.3 adds no new job queue, lease, heartbeat,
recovery pipeline, or memory system of its own. Every capability below is built by reusing V0.1's
runtime (`mainai_jobs`, leases, checkpoints, verification, approval, final report) and V0.2's
recovery pipeline (detect → inspect → classify → takeover) unchanged, wiring NEW decisions and
NEW automatic triggers on top of them.

V0.2's own "V0.3 CANDIDATES" section named the gaps this branch closes: a background pass that
notices dead `task_execution` jobs automatically (was entirely founder-triggered), and —
separately, from V0.1's own scope notes — a task that genuinely needs to wait on something
external (a CI run) rather than finishing in one shot, a way to actually stop a `running` task's
real work instead of only ever refusing to cancel it, a task that keeps failing getting retried
without a founder manually clicking retry every time, a goal whose plan turns out to be wrong
getting replanned without a founder noticing and re-triggering it by hand, and lessons that
silently contradict each other never being flagged. V0.3's shape is six once-manual decisions
made durable and automatic, each one **reusing** an existing V0.1/V0.2 primitive rather than
building a parallel one:

1. **CI-wait** — a task with a real, pushed GitHub commit does not count as done until its
   checks conclude; `waiting_ci` is a new task status, not a new job/lease concept.
2. **Cooperative cancellation** — a `running` task's own job checks `cancel_requested`
   (`app/jobs/service.py`'s existing primitive, unchanged) at real safe checkpoints.
3. **Retry-with-backoff** — the SAME `executor.retry_task()` a founder's manual retry already
   used, now called automatically once `next_retry_at` elapses.
4. **Automatic dead-agent recovery** — the SAME four V0.2 functions
   (`get_or_create_recovery_record` → `inspect_recovery_record` → `classify_recovery_record` →
   `execute_takeover`) a founder's `POST /tasks/{id}/recover` already called, now driven by a
   worker tick that finds dead `task_execution` jobs itself.
5. **Minimal replan trigger** — the SAME `propose_plan_via_ai()`/`create_plan()` a founder's
   `POST /goals/{id}/plan` already called, now triggered automatically by a permanently failed
   task.
6. **Engineering-lesson conflict detection** — a new, small, two-stage judgment
   (deterministic pairing, then one real AI call) that disputes two lessons whose
   `general_rule`/`fix` genuinely contradict each other, closing a gap V0.1's own
   `EngineeringLessonConfidence` docstring named as absent.

## Why these six, together, as one V0.3

All six share the same underlying shape: **a decision a founder used to have to make (or
notice) by hand now happens unattended, on a worker tick, using the exact same underlying
primitive the founder's own manual action already used.** None of them introduce a second way
to dispatch a job, resolve a task, grant an approval, or judge a lesson — they only decide WHEN
the existing mechanism should run.

## Architecture at a glance

```
mainai_jobs row (task_execution, running)
        │
        ▼  _check_cancel_requested() at 3 safe checkpoints   -- cooperative cancel (#2)
        ▼  real work (AI call, file write, verify, push)
        │
        ├─ open_pr + verification passed + real commit pushed
        │       ▼  start_ci_wait()                            MainAITaskWait (pending)
        │       ▼  mainai_jobs row -> completed (its own work IS done)
        │       ▼  task -> waiting_ci (NOT done yet)
        │              │
        │              ▼  worker._poll_mainai_task_waits()     -- CI-wait (#1)
        │              ▼  poll_ci_wait() -> satisfied/failed/timed_out
        │              ▼  resume_waiting_ci_task() -> _finalize_task_outcome()
        │
        └─ verification failed, attempts remain
                ▼  task -> retryable_failed, next_retry_at scheduled
                ▼  worker._advance_mainai_execution_retries()   -- retry-with-backoff (#3)
                ▼  executor.retry_task() -> task -> ready -> redispatched next tick

mainai_jobs row (task_execution, running, lease EXPIRED)
        ▼  worker._advance_mainai_execution_auto_recovery()     -- auto dead-agent recovery (#4)
        ▼  SAME V0.2 pipeline: detect -> inspect -> classify -> [approval gate] -> takeover

goal's active plan has a permanently `failed` task
        ▼  worker._advance_mainai_execution_replan()            -- minimal replan trigger (#5)
        ▼  SAME propose_plan_via_ai()/create_plan() -> new plan version, `replanned` event

active EngineeringLesson set
        ▼  worker._resolve_engineering_lesson_conflicts()       -- lesson conflict detection (#6)
        ▼  find_conflict_candidate_pairs() -> detect_conflict() (1 real AI call) -> mark_conflict()
```

Every new table/column/event type below was added in migration 0036 (`mainai_task_waits`,
`mainai_tasks.next_retry_at`, new `MainAITaskEventType` values). See `app/models/mainai_wait.py`
for the wait table's full column-level rationale.

## Module map

| Module | Responsibility |
|---|---|
| `app/models/mainai_wait.py` | `MainAITaskWait`, `MainAITaskWaitSourceType` (closed vocabulary, only `github_check_runs` implemented), `MainAITaskWaitStatus` |
| `app/mainai_execution/ci_wait.py` | `start_ci_wait()`, `evaluate_check_runs()` (pure), `poll_ci_wait()` (real GitHub Checks API call, stale-SHA/repo-drift fail-closed), `cancel_ci_wait()` |
| `app/mainai_execution/execution_job.py` | `_check_cancel_requested()`/`TaskCancelledCooperatively` (cancellation checkpoints), `_finalize_cancelled_task()`, `resume_waiting_ci_task()` (locks task first — see SECURITY INVARIANTS), CI-wait branch inside `run_task_execution_job()`, `next_retry_at` scheduling inside `_finalize_task_outcome()` |
| `app/mainai_execution/executor.py` | `cancel_task()` rewritten for three genuinely different mechanisms (pending/ready/blocked/retryable_failed vs. `waiting_ci` vs. `running`); `retry_task()` clears `next_retry_at` |
| `app/mainai_execution/replan.py` | `find_replan_trigger()` (deterministic — first permanently-failed task in the ACTIVE plan only), `trigger_replan()` (reuses `propose_plan_via_ai()`/`create_plan()`) |
| `app/mainai_execution/lesson_conflicts.py` | `find_conflict_candidate_pairs()` (deterministic narrowing), `detect_conflict()` (the one AI judgment call, fail-closed), `mark_conflict()` (disputes both, records `lesson_conflict_detected` events), `resolve_conflicts_among()` |
| `app/worker.py` | Five new bounded ticks: `_poll_mainai_task_waits`, `_advance_mainai_execution_retries`, `_advance_mainai_execution_auto_recovery`, `_advance_mainai_execution_replan`, `_resolve_engineering_lesson_conflicts` |
| `app/mainai_execution/final_report.py` | `_wait_history()`, `_lesson_conflict_evidence()`, `next_retry_at`/`execution_attempt.cancel_requested`/`triggered_replan` surfaced per-task; goal-summary rollups |
| `app/routers/mainai_execution.py` | Three new founder-only endpoints (see MINIMAL API) |

## REAL

- **CI-wait is a real, durable status, not a sleep loop.** `waiting_ci` (migration 0036) is a
  genuine `MainAITaskStatus`; the `mainai_jobs` row that opened the PR reaches its own terminal
  `completed` at that point (its own work — open the PR — IS done), while the TASK stays
  `waiting_ci` until a separate worker tick resolves it. Polling always re-reads the wait's own
  durable `(repo, sha)` — never a caller-supplied SHA, never "the branch's current tip" — and
  refuses to poll at all if the backend's currently-configured `github_repo` no longer matches
  the wait's own stored repo (`ci_wait.py`'s `poll_ci_wait()`).
- **Real fail-closed check-run evaluation.** `evaluate_check_runs()` treats "no check runs yet"
  as NOT done (never silently green — a repo with no CI configured is indistinguishable from
  "not started yet" from this API alone), and an unrecognized GitHub Checks conclusion string as
  NOT passed once done — a future GitHub API addition this code has never seen is never silently
  interpreted as success.
- **Real cooperative cancellation at three genuine safe checkpoints inside
  `run_task_execution_job()`**: before any real work starts, after a real local commit but
  before verification, and after verification but before the one NOT-safely-repeatable step (a
  real `git push`). Uses the SAME `job.cancel_requested`/`request_cancel()` primitive
  `corpus_review.py`'s own per-batch cancel check already used — no second cancellation
  mechanism. A cancel arriving mid-flight preserves whatever real, already-durable work exists
  (a local commit is never discarded) and never pushes.
- **Real three-way `cancel_task()`**: pending/ready/blocked/retryable_failed cancel immediately
  (unchanged V0.1/V0.2 behavior); `waiting_ci` cancels immediately too (no job is in flight —
  the durable `MainAITaskWait` is cancelled via `cancel_ci_wait()` so a later poll tick can never
  resurrect it); `running` is genuinely cooperative — sets `cancel_requested` and waits for the
  job's own next safe checkpoint to acknowledge it. All three routed through the SAME entry
  point, not a parallel system.
- **Real automatic retry-with-backoff.** `_finalize_task_outcome()` schedules a real
  `next_retry_at` (full-jitter exponential backoff, `app/jobs/retry.py`'s existing formula,
  30s/900s task-appropriate base/cap) whenever a task fails with attempts remaining; a bounded
  worker tick (`_advance_mainai_execution_retries`, `_MAX_RETRY_SCANS_PER_TICK = 25`) calls the
  SAME `executor.retry_task()` a founder's manual retry endpoint already used once that time
  elapses — the task is picked up and actually redispatched by the very next tick's existing
  `_advance_mainai_execution_tasks`, with no duplicated dispatch logic.
- **Real automatic dead-agent recovery.** A bounded worker tick
  (`_advance_mainai_execution_auto_recovery`, `_MAX_AUTO_RECOVERY_SCANS_PER_TICK = 10`) finds
  `task_execution` jobs that are `running` with an expired lease and drives them through the
  EXACT SAME four V0.2 functions a founder's `POST /tasks/{id}/recover` already calls — no
  bypass of `execute_takeover()`'s own approval gate: PUSHED_NO_PR/PR_EXISTS still raise
  `RecoveryApprovalRequiredError` here exactly as they do for a founder-triggered call, caught
  and left for a founder to approve via the existing API, never auto-approved by this tick.
  CONFLICTED_STATE/UNSAFE_TO_AUTO_RECOVER are never in `AUTO_SALVAGEABLE_CLASSIFICATIONS`, so
  this tick never even attempts a takeover for them — classification alone still runs and
  durably records `manual_review_required=True` for a founder to find.
- **Real minimal replan trigger.** `find_replan_trigger()` is a pure, deterministic query:
  the first permanently `failed` task in a goal's CURRENTLY ACTIVE plan only — a failed task
  from an already-superseded plan (handled by that plan's own supersession) never re-triggers.
  `trigger_replan()` calls the SAME `propose_plan_via_ai()`/`create_plan()` a founder's manual
  replan already used, unchanged — supersession, stale-task cancellation, and re-promoting
  dependency-free tasks to `ready` all happen exactly as they always have. Approval escalation
  needs no new mechanism: replanned tasks get brand-new ids, so a prior `approval_granted` event
  (keyed by task id) can structurally never carry over.
- **Real engineering-lesson conflict detection.** `find_conflict_candidate_pairs()` is a pure,
  deterministic narrowing (both `active`, same `affected_component`, at least one shared
  `applies_to` tag) — two lessons about unrelated components are never even compared.
  `detect_conflict()` is the one genuine judgment call (same `chat_with_fallback()` provider
  chain every other AI judgment in this codebase already uses), fails CLOSED on a malformed or
  errored response (never silently marks two lessons `disputed` on an unreliable signal, but
  does log it so the underlying problem stays visible). `mark_conflict()` disputes BOTH lessons
  (never picks a winner) and records a `lesson_conflict_detected` event on every task whose
  verification plan actually applied either one (a real JSONB containment query over
  `mainai_task_events`, not a Python-side scan) — so a founder reviewing an affected task's
  final report sees exactly why a disputed lesson matters to THAT task.
- **Real final-report integration** for all six capabilities: `wait_history`, `next_retry_at`,
  `execution_attempt.cancel_requested`/`cancel_acknowledged`, `triggered_replan`,
  `lesson_conflicts` per task; `plan_versions_total`, `tasks_with_wait_history`,
  `tasks_awaiting_auto_retry`, `tasks_with_disputed_lesson_evidence` in the goal summary.
  `unresolved_risk` was corrected: `retryable_failed` is REMOVED from
  `_UNRESOLVED_TASK_STATUSES` (a scheduled automatic retry is no longer "awaiting a founder
  decision" the way V0.1 described it) and `unresolved_lesson_conflict` was ADDED (a task that
  relied on a since-disputed lesson is real, actionable evidence regardless of the task's own
  status).
- **Real, minimal, founder-only API additions** (`GET /goals/{id}/plans`, `GET /tasks/{id}/waits`,
  `GET /lessons`) that read real durable state through HTTP, never a shortcut.
- **A real, previously-unguarded concurrency race was found and closed this branch**:
  `resume_waiting_ci_task()` originally read the task via `db.get()` instead of this codebase's
  established `_lock_task()` primitive (already used by
  `dispatch_ready_task()`/`retry_task()`/`cancel_task()`). Two concurrent worker processes both
  polling the same due wait could otherwise both observe `waiting_ci`, both poll GitHub, and
  both call `_finalize_task_outcome()` on the same task — a double-dispatch-shaped bug. Fixed by
  locking the task row first, then rechecking status; verified via mutation testing (reverting
  the fix made the regression test fail reliably 3/3 runs).
- All 9 of the founder's required demo scenarios pass through the real production paths above
  — see DEMO RESULTS.

## STUBBED

Nothing in this list. Every piece named above does real work against real state.

## LIMITED

Named explicitly rather than silently left implicit:

1. **CI-wait has exactly one source type.** `MainAITaskWaitSourceType` is a real closed
   vocabulary (CHECK-enforced, not free text) specifically so a second source could be added
   later without a schema change, but only `github_check_runs` is implemented — the founder's
   own "general but minimal" instruction for this table, not evidence a second source exists.
2. **CI-wait's condition evaluator supports exactly one condition.** `condition={}` always means
   "every check run must conclude success" — there is no per-wait configuration for e.g. only
   watching a named subset of checks.
3. **Cooperative cancellation has exactly three checkpoints**, matching `run_task_execution_job()`'s
   own three natural stopping points (before work, after commit, after verification/before
   push). A task type with a longer or different internal sequence would need its own checkpoint
   placement — this is not a general "cancel anywhere" primitive. **Named precisely (hardening-
   pass finding, §12 attack)**: no checkpoint interrupts a task_type handler already in flight —
   a `run_tests` task's real `subprocess.run()` pytest invocation (or a `repo_edit`/`run_tests`
   task's `targeted_tests` verification step) always runs to its own natural completion (pass,
   fail, or its own internal timeout) once started; a cancel that lands while it's running is
   picked up only at the NEXT checkpoint afterward, never mid-subprocess. Proven directly: the
   subprocess's real result is still checkpointed and never corrupted, but the task still
   correctly finalizes `cancelled`, never `completed`, once the next checkpoint runs (see
   `test_cancel_arriving_while_a_real_subprocess_is_running_lets_it_finish_then_stops_at_the_next_checkpoint`,
   `tests/backend/test_mainai_execution_cancellation.py`).
4. **The replan trigger looks at exactly one signal**: a permanently `failed` task in the
   currently active plan. It does not consider partial progress, cost, or how many times a goal
   has already been replanned — a goal could in principle replan repeatedly if each new plan's
   own task fails the same way (see KNOWN RISKS).
5. **Lesson conflict detection only compares lessons sharing BOTH `affected_component` and an
   `applies_to` tag.** Two lessons that genuinely contradict each other about, e.g., a shared
   underlying library but different named components would never be compared — a deliberately
   narrow, low-false-positive net, not a general contradiction detector.
6. **No in-app resolution workflow for a disputed lesson pair.** `mark_conflict()` disputes both
   and stops — a founder must resolve which (if either) returns to `active` entirely out of
   band (directly editing the row); there is no "resolve and retry" button, same limitation
   V0.2's own CONFLICTED_STATE handling named.
7. **Retry-with-backoff and auto-recovery are both bounded per-tick scans**
   (`_MAX_RETRY_SCANS_PER_TICK = 25`, `_MAX_AUTO_RECOVERY_SCANS_PER_TICK = 10`,
   `_MAX_CI_WAITS_PER_TICK = 25`, `_MAX_REPLAN_SCANS_PER_TICK = 10`,
   `_MAX_LESSON_CONFLICT_SCANS_PER_TICK = 50`) — a real, deterministic-ordering constraint
   (proven by dedicated tests), not an unbounded scan, but a goal/task/wait/job past its own
   tick's bound simply waits for the next cycle rather than being starved forever by a design
   flaw; at V0.3's scale this is a deliberate simplicity choice.
   **Fairness, precisely (hardening-pass finding, §7 attack)**: every one of these queries
   orders strictly by due-time (`next_poll_at`/`next_retry_at`/`lease_expires_at` ascending,
   `id` ascending as a tiebreak) with NO `goal_id`/owner grouping at all. This is real, honest
   **temporal FIFO** (oldest-due-first, monotonically draining, mathematically never a
   permanent starve of a finite backlog) — it is explicitly NOT per-goal round-robin fairness.
   Proven directly: one goal with a backlog larger than a single tick's own bound can and does
   delay a DIFFERENT goal's own newly-due, single item by roughly
   `ceil(backlog_size / tick_limit)` ticks, even though that item is individually more urgent
   in wall-clock terms (see
   `test_retry_tick_is_fifo_by_due_time_not_fair_across_goals_and_the_doc_must_say_so`,
   `tests/backend/test_mainai_execution_retry_tick.py`). Named explicitly here rather than
   implying an unverified per-goal fairness guarantee.

## NOT IMPLEMENTED

- Any UI beyond the backend API plus the existing admin page's incremental extensions (plan
  history, wait history, disputed-lessons banner) — no new frontend route was added, per the
  founder's own "minimal API/UI" scope precedent from V0.1/V0.2.
- A second named recovery-approval policy, a second cancellation mechanism, or a second retry
  policy — V0.3 deliberately reuses every V0.1/V0.2 decision mechanism unchanged rather than
  building a parallel one (see "Why these six, together" above).
- Force-push, destructive overwrite, or any recovery/replan path that guesses past unverifiable
  state — deliberately excluded, same standing instruction V0.2 already honored.
- Deploy, VPS provisioning, production migration/backfill, or any change outside this backend/
  frontend branch's own scope — out of scope for V0.3 by explicit founder mandate.

## KNOWN RISKS

- **A goal could in principle replan repeatedly** if each new plan version's own task fails the
  same underlying way (LIMITED #4) — `find_replan_trigger()` has no cap on how many times a
  single goal replans; at V0.3's scale this is a named, not-yet-mitigated risk, not a silent gap.
- **Lesson conflict detection's narrow matching (LIMITED #5)** means some genuine contradictions
  are never flagged — a deliberate low-false-positive tradeoff, but real coverage risk worth
  naming rather than implying completeness.
- **No in-app resolution workflow for disputed lessons (LIMITED #6)** — a disputed pair stays
  disputed (excluded from `lookup_lessons()`'s `active`-only results) until a founder resolves
  it entirely out of band; there is no reminder or escalation if that never happens.
- **Bounded per-tick scans (LIMITED #7)** mean a system with more due waits/retries/dead
  jobs/goals/lessons than a single tick's bound in one poll cycle will process the rest on
  later cycles — correct, not lossy, but a founder should not expect same-tick processing at
  volumes well beyond V0.3's tested scale.
- **Scheduler query index strategy analyzed, not load-tested at production scale (hardening-pass
  finding, §25).** All five bounded scans (waits/retries/auto-recovery/replan/lesson-conflict)
  filter on an indexed `status` (or `job_type`+`status`) column with an additional due-time
  filter (`next_poll_at`/`next_retry_at`/`lease_expires_at`, each independently indexed too).
  `EXPLAIN` on the current (near-empty) test database shows the planner choosing the `status`
  index, which is the right choice at this data volume. This is expected to remain correct at
  real scale too, since these `status` values are a small enumerated set that most rows leave
  quickly (pending → satisfied/failed/timed_out; retryable_failed → ready; running → completed/
  superseded) — a genuinely large, sustained backlog in one of these statuses would itself be a
  real operational signal worth alerting on, not just a query-plan concern. Not verified against
  a synthetic multi-thousand-row population under real Postgres statistics — named here as an
  honest gap rather than an unverified performance claim.

## SECURITY INVARIANTS

- `mainai_task_waits` is owner-scoped RLS (`FORCE ROW LEVEL SECURITY`), same convention as every
  other V0.1/V0.2 execution table (migration 0036); an ordinary mutable table (a poll updates
  its own row in place), not append-only, since it is not an event log.
- CI-wait polling always re-reads the wait's OWN durable `(repo, sha)` from the row — never a
  caller-supplied value, never "the branch's current tip" — and refuses to poll at all if the
  backend's currently-configured `github_repo` has drifted from the wait's own stored repo,
  closing a stale-SHA/repo-drift class of bug where a different commit's checks could otherwise
  be mistaken for this task's own.
- `resume_waiting_ci_task()` locks the task row (`_lock_task()`) before checking status — closes
  a genuine double-finalize race between two concurrent worker processes polling the same due
  wait, proven by a real two-thread/two-session concurrency test, mutation-verified.
- `cancel_task()`'s `running` branch requires both a real `mainai_job_id` AND a real
  `cancelled_by_id` — never sets `cancel_requested` on a task with no job in flight, and never
  as an anonymous action (matching `request_cancel()`'s own signature).
- The V0.3 auto-recovery tick never bypasses `execute_takeover()`'s own approval gate — an
  unapproved PUSHED_NO_PR/PR_EXISTS record raised by the automatic tick is caught and rolled
  back, left for a founder to approve via the existing API, exactly as it would be for a
  founder-triggered call; the tick has no separate, weaker approval path.
- `find_conflict_candidate_pairs()`/`detect_conflict()` never mutate anything themselves — only
  `mark_conflict()` writes, and only after a real AI judgment call, fail-closed on any error.
- `GET /lessons` requires `require_founder` like every other endpoint in this router even though
  `EngineeringLesson` itself is deliberately not owner-scoped (founder-wide knowledge, unchanged
  V0.1 design) — never accidentally exposed to a non-founder caller.
- `GET /goals/{id}/plans` and `GET /tasks/{id}/waits` reuse the SAME `_get_goal_or_404`/
  `_get_task_or_404` helpers every other owner-scoped endpoint in this router already uses (RLS
  enforced through the ordinary session, never re-derived in application code) — explicitly
  spot-checked with real cross-owner data (not just an unknown-id 404), not merely inferred from
  shared-helper reuse.

## DURABILITY INVARIANTS

- A `waiting_ci` task's owning `mainai_jobs` row reaches `completed` the moment the PR is
  genuinely opened — the job's own work IS done; only the TASK's own status stays non-terminal,
  so a crash after this point never leaves a job stuck at a lease-expired `running` with nothing
  actually happening.
- `poll_ci_wait()` is idempotent to call on an already-terminal wait (a no-op returning it
  unchanged) — a caller never needs to check status first.
- A cooperative cancel's own commit ordering matches every other outcome path in
  `execution_job.py`: this function's own write is flushed first, `mark_cancelled()`'s commit
  happens last, so a crash between the two loses nothing durable either way.
- `_finalize_cancelled_task()` is a genuinely separate terminal path from
  `_finalize_task_outcome()` — a cancel never counts against `task.attempts`, never schedules a
  `next_retry_at`, and never records a `verification_failed` event that didn't actually happen.
- `retry_task()` (both the manual and now the automatic-tick call site) clears `next_retry_at`
  unconditionally — a stale scheduled retry can never fire twice for the same failure.
- A `MainAITaskEventType.replanned` event is recorded on the SPECIFIC failed task that triggered
  a replan, durably linking "this failure" to "this new plan version" — not a generic goal-level
  note.
- `mark_conflict()` never deletes a lesson's history — both disputed lessons keep every prior
  field, same no-delete-based-forgetting discipline every other status transition in this table
  already follows.

## EVENT VOCABULARY (migration 0036)

New `MainAITaskEventType` values, append-only like every other task event:

| Event | Recorded by |
|---|---|
| `wait_started` | `ci_wait.py`'s `start_ci_wait()` |
| `wait_satisfied` | `execution_job.py`'s `resume_waiting_ci_task()`, on a passing CI outcome |
| `wait_timed_out` | `resume_waiting_ci_task()`, once `deadline_at` elapses with no conclusion |
| `cancel_requested` | `executor.py`'s `cancel_task()`, on a `running` task |
| `cancelling` | `execution_job.py`'s `_finalize_cancelled_task()`, the in-flight acknowledgment |
| `cancelled` (task-level) | `_finalize_cancelled_task()` / `cancel_task()`'s immediate branches |
| `auto_recovery_triggered` | `worker.py`'s `_advance_mainai_execution_auto_recovery`, before an automatic takeover attempt |
| `lesson_conflict_detected` | `lesson_conflicts.py`'s `mark_conflict()`, on every task that applied a now-disputed lesson |

`replanned` and `retry_scheduled` already existed as `MainAITaskEventType` values from V0.1;
V0.3 is the first time either is actually recorded by a real, automatic code path.

## DEMO RESULTS

All 9 of the founder's required scenarios are real, automated tests (not manual walkthroughs),
run through the actual production paths described above — never a manual shortcut that fakes
evidence directly.

1. **`open_pr` dispatch lands on `waiting_ci`, not `completed`** —
   `test_demo_open_pr_real_dispatch_lands_on_waiting_ci_not_completed`
   (`test_mainai_execution_ci_wait.py`): a real dispatch through `run_task_execution_job()`
   opens a real PR; the task ends `waiting_ci`, the job ends `completed`. **PASSED.**
2. **CI-wait resolves green and completes the task** —
   `test_demo_ci_wait_resolves_green_and_completes_the_task`: a real worker poll tick
   (`resume_waiting_ci_task()`) observes a passing check-run conclusion and drives the task to
   `completed` via the same `_finalize_task_outcome()` every other task_type uses. **PASSED.**
3. **CI-wait resolves red and schedules a backoff retry** —
   `test_demo_ci_wait_resolves_red_and_schedules_a_backoff_retry`: a failing check-run
   conclusion drives the task to `retryable_failed` with a real `next_retry_at` scheduled, never
   falsely `completed`. **PASSED.**
4. **CI-wait times out and never falsely completes** —
   `test_demo_ci_wait_times_out_and_never_falsely_completes`: a wait whose `deadline_at` has
   elapsed with no conclusion reaches `timed_out`, never silently treated as a pass. **PASSED.**
5. **Cancel requested before dispatch stops before any AI call** —
   `test_demo_cancel_requested_before_dispatch_stops_before_any_ai_call`
   (`test_mainai_execution_cancellation.py`): `cancel_requested` set before the job's first
   checkpoint means the real AI provider is never called at all (call count asserted == 0).
   **PASSED.**
6. **Cancel arriving mid-flight preserves the already-made local commit and never pushes** —
   `test_demo_cancel_arriving_mid_flight_preserves_the_already_made_local_commit_and_never_pushes`:
   a real local git commit made before the cancel request is honored survives; the real `git
   push` never happens (push call count asserted == 0). **PASSED.**
7. **Auto-recovery tick detects and takes over a dead job with no founder action** —
   `test_demo_auto_recovery_tick_detects_and_takes_over_a_dead_job_with_no_founder_action`
   (`test_mainai_execution_auto_recovery.py`): a real dead `task_execution` job (expired lease)
   is found, inspected, classified, and taken over entirely by the worker tick — no manual
   `POST /recover` call. **PASSED.**
8. **Auto-replan triggers after a task permanently exhausts retries** —
   `test_demo_auto_replan_triggers_after_a_task_permanently_exhausts_retries`
   (`test_mainai_execution_replan.py`): a real failing pytest run (`max_attempts=1`) exhausts a
   task's retry budget through `run_task_execution_job()`; the worker's own replan tick — no
   manual call — proposes and persists a fresh plan version, records a `replanned` event, and
   the new plan's tasks are genuinely new ids (no stale approval carryover). **PASSED.**
9. **Worker tick disputes conflicting lessons and the planner no longer applies them** —
   `test_demo_worker_tick_disputes_conflicting_lessons_and_planner_no_longer_applies_them`
   (`test_mainai_execution_lesson_conflicts.py`): two real, genuinely contradicting lessons are
   disputed by the worker's own conflict-resolution tick; a subsequent real
   `apply_lessons_to_verification_plan()` call no longer applies either one. **PASSED.**

## MINIMAL API

Three new founder-only endpoints added to the existing V0.1/V0.2 router
(`app/routers/mainai_execution.py`); the existing `POST /tasks/{id}/cancel` and
`POST /tasks/{id}/retry` endpoints now cover the `waiting_ci`/`running` cancel branches and the
automatic-retry-cleared `next_retry_at` respectively, with no new endpoint needed for either:

| Endpoint | Behavior |
|---|---|
| `GET /goals/{id}/plans` | Every plan version a goal has ever had, oldest first (the replan history) |
| `GET /tasks/{id}/waits` | Every durable external-wait record a task has ever gone through |
| `GET /lessons` | Founder-wide engineering lessons, optional `status_filter` (e.g. `disputed`) |

Deliberately backend-plus-incremental-admin-UI for V0.3 — the existing
`/admin/mainai-execution` page gained a plan-history view, a wait-history section, and a
founder-wide disputed-lessons banner; no new frontend route was added.

## V0.4 CANDIDATES

Documented, not built, per the same "name it, don't build it yet" discipline V0.1/V0.2's own
documents used:

- **A cap on repeated replans per goal** (KNOWN RISKS), so a goal whose new plan fails the same
  way cannot replan indefinitely without a founder noticing.
- **A second `MainAITaskWaitSourceType`** beyond `github_check_runs` (e.g. waiting on an
  external approval, a scheduled time, another goal's completion) — the table's own vocabulary
  was built general-but-minimal specifically so this needs no schema change.
- **A configurable CI-wait condition** beyond "every check run must pass" (e.g. only a named
  subset of required checks).
- **An in-app resolution workflow for a disputed lesson pair** (LIMITED #6), mirroring V0.2's
  own still-open CONFLICTED_STATE candidate.
- **Wider lesson-conflict matching** than the current `affected_component` + shared
  `applies_to` tag narrowing (LIMITED #5), if false negatives prove to matter in practice.

## Coverage matrix

| Requirement | Covered by |
|---|---|
| `waiting_ci` reachable from a real dispatch | `test_demo_open_pr_real_dispatch_lands_on_waiting_ci_not_completed` |
| Fail-closed check-run evaluation (no runs yet, unrecognized conclusion) | `test_mainai_execution_ci_wait.py` (`evaluate_check_runs` unit section) |
| Stale-SHA / repo-drift defense | `test_mainai_execution_ci_wait.py` (poll section) |
| CI-wait green/red/timeout outcomes | Demos 2/3/4 |
| Double-finalize race on a due wait | `test_resume_waiting_ci_task_concurrent_same_wait_never_double_finalizes` (mutation-verified) |
| Cancellation: pending/ready/blocked/retryable_failed immediate | `test_mainai_execution_cancellation.py` |
| Cancellation: `waiting_ci` cancels the wait too | `test_mainai_execution_cancellation.py` |
| Cancellation: `running` is cooperative, 3 checkpoints | Demos 5/6 |
| Retry-with-backoff scheduling | `test_mainai_execution_retry_tick.py` |
| Retry tick never touches a not-yet-due task | `test_retry_tick_never_touches_a_task_whose_next_retry_at_has_not_yet_elapsed` |
| Retry tick bounded scan | `test_retry_tick_bounds_how_many_tasks_it_touches_per_cycle` |
| Auto-recovery: dead job detected and taken over unattended | `test_demo_auto_recovery_tick_detects_and_takes_over_a_dead_job_with_no_founder_action` |
| Auto-recovery: approval gate never bypassed | `test_mainai_execution_auto_recovery.py` |
| Replan trigger: deterministic, active-plan-only scoping | `test_find_replan_trigger_ignores_a_failed_task_in_an_already_superseded_plan` |
| Replan trigger: `retryable_failed` is not a trigger | `test_find_replan_trigger_ignores_a_retryable_failed_task` |
| Auto-replan end to end | `test_demo_auto_replan_triggers_after_a_task_permanently_exhausts_retries` |
| Lesson conflict: deterministic candidate narrowing | `test_mainai_execution_lesson_conflicts.py` (candidate pairing section) |
| Lesson conflict: fail-closed AI judgment | `test_mainai_execution_lesson_conflicts.py` (malformed/erroring provider section) |
| Lesson conflict: both disputed, task-level events recorded | `test_mainai_execution_lesson_conflicts.py` (`mark_conflict` section) |
| Lesson conflict end to end, planner stops applying disputed lessons | `test_demo_worker_tick_disputes_conflicting_lessons_and_planner_no_longer_applies_them` |
| Final report: wait/retry/cancel/replan/lesson-conflict integration | `test_mainai_execution_final_report_v0_3.py` |
| Final report: `unresolved_risk` no longer flags a scheduled retry | `test_next_retry_at_surfaces_and_is_no_longer_unresolved_risk` |
| Final report: disputed-lesson evidence IS an unresolved risk | `test_lesson_conflicts_surface_a_real_disputed_lesson` |
| API: `GET /goals/{id}/plans`, `GET /tasks/{id}/waits`, `GET /lessons` (200 + 404 + validation) | `test_mainai_execution_api.py` (section E) |
| API: cross-owner isolation for the two new owner-scoped endpoints | `test_api_a_founder_cannot_list_another_owners_goal_plans`, `test_api_a_founder_cannot_list_another_owners_task_waits` |
| Migration round-trip (0036) | `test_migration_roundtrip.py` |
| `mainai_task_waits` RLS + privilege policy | `app/rls.py`'s `_MAINAI_EXECUTION_TABLES` inclusion, existing privilege-policy test suite |
| No force push, ever | Unchanged from V0.2 — no code path in this branch passes `--force` |

## Test suite

New/substantially-extended test files added by this branch:

- `tests/backend/test_mainai_execution_ci_wait.py` (22 tests, incl. the double-finalize
  concurrency regression test, the cancel-vs-in-flight-poll concurrency proof, the
  resource_ref fail-closed test, a direct-SQL cross-owner RLS attack test, and the recorded
  engineering lesson — all added during the hardening/attack pass)
- `tests/backend/test_mainai_execution_cancellation.py` (6 tests, incl. both required demos
  and the real-subprocess cooperative-cancellation test added during the hardening pass)
- `tests/backend/test_mainai_execution_retry_tick.py` (5 tests, incl. the FIFO-fairness proof
  added during the hardening pass)
- `tests/backend/test_mainai_execution_auto_recovery.py` (3 tests, incl. the required demo)
- `tests/backend/test_mainai_execution_replan.py` (5 tests, incl. the required demo and the
  CRITICAL approval-escalation-across-replan test added during the hardening pass)
- `tests/backend/test_mainai_execution_lesson_conflicts.py` (11 tests, incl. the required demo)
- `tests/backend/test_mainai_execution_final_report_v0_3.py` (6 tests)

Plus, in `tests/backend/test_mainai_execution_api.py` (V0.1's own file, extended): a new
section E (plans/waits/lessons endpoints, 7 tests) and two cross-owner isolation tests added
during the hardening/security-attack pass; and `tests/backend/test_mainai_execution_demos.py`
(V0.1's own file), where Demo 2's assertions were updated to reflect V0.3's corrected
`unresolved_risk` semantics (a `retryable_failed` task with a scheduled retry is no longer an
unresolved risk).

## Hardening / attack pass (post-PR #59 draft review)

Same build → freeze → harden → merge model as V0.1/V0.2. Full diff re-review against every
attack category the founder named (wait state machine, CI SHA/repo binding, double-wake
concurrency, crash matrix, scheduler bounds/fairness, retry/side-effect dedup, cancellation/
stale-worker/subprocess termination, auto-recovery/takeover fencing, replan/approval
escalation, lesson-conflict safety, event integrity, RLS/privileges, API, final report
truthfulness, migration/performance, mutation coverage, doc truthfulness). Real findings:

- **Fixed (P3, fail-closed hardening)**: `poll_ci_wait()`'s repo-drift guard used
  `if repo and client.settings.github_repo != repo`, which silently skipped the identity check
  entirely if the wait's own stored `repo` (or `sha`) were ever falsy, instead of refusing to
  poll. Fixed to fail closed on either missing field, matching the "no optimistic success"
  principle the rest of this module already follows.
- **Verified correct, not changed (near-miss, engineering lesson recorded)**:
  `resume_waiting_ci_task()` holds the task row lock across `poll_ci_wait()`'s real GitHub
  network round-trip. A tempting "poll first, lock only for the write" reordering was analyzed
  and found to introduce a genuinely NEW race (a poll already in flight when a cancel lands
  could still overwrite the wait row's `cancelled` status back to `satisfied`/`failed` after
  the cancel's own commit) — the reordering was never made. A real two-thread concurrency test
  now proves the CURRENT design is safe under a real resume-vs-cancel race, and was
  mutation-verified against the exact reordering that was considered and rejected.
- **Verified correct, newly tested**: approval escalation across an automatic replan (§16,
  flagged CRITICAL) — a v1 approval can never apply to a v2 task even when the new task also
  requires approval, proven end to end through the real `trigger_replan()`, mutation-verified.
- **Verified correct, newly tested**: cooperative cancellation while a real subprocess
  (`run_tests`' pytest invocation) is genuinely in flight — the subprocess always runs to its
  own natural completion, never interrupted mid-flight; the cancel is only picked up at the
  next checkpoint afterward. Named explicitly in the docs (was previously only implied).
- **Verified and precisely characterized, not previously proven**: scheduler fairness (§7) —
  every bounded tick query is real temporal FIFO (oldest-due-first, ordered by due-time with no
  goal/owner grouping at all), not per-goal round-robin fairness. Proven directly: one goal's
  large backlog measurably delays a different goal's own newly-due item by roughly
  `backlog_size / tick_limit` ticks. The doc's own LIMITED #7 was tightened to state this
  precisely rather than leaving it implied.
- **Verified, no gap found**: `mainai_task_waits` RLS under the real runtime role (direct SQL
  cross-owner attack, matching V0.1's own established pattern for this table for the first
  time) and its Postgres privilege floor (TRUNCATE/REFERENCES/TRIGGER never granted — covered
  automatically by the existing global, table-name-agnostic privilege test).
- Every other attacked area (retry-scheduling dedup, auto-recovery approval-gate bypass
  resistance, lesson-conflict fail-closed AI judgment, event append-only enforcement, migration
  0036's CHECK/FK/RLS/index shape, API authority boundaries, final report truthfulness) was
  re-attacked against the actual current code and confirmed already correctly guarded by the
  build-phase's own work — no further P0/P1/P2 findings.

Run the full backend suite: `pytest tests/`. Last full run after the hardening pass: 1328
passed, 1 skipped (the P2 capacity test, skipped by design), 1 failed
(`test_write_stream_vs_delete_never_returns_a_blob_missing_from_disk`, a storage-layer race
test — confirmed genuinely flaky by rerunning in isolation immediately after, passing;
pre-existing, unrelated to this branch's diff, which never touches `app/storage/` or
`tests/backend/storage/`).
