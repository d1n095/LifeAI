# MainAI Dead Agent Recovery V0.2

Branch: `claude/mainai-dead-agent-recovery-v0-2`. This document describes the IMPLEMENTED
reality of V0.2 as of this branch's head — not the aspiration, not what a later version will
add. Where something is stubbed, limited, or simply not built, it is named as such below
rather than implied to work. Written in the same discipline as
`backend/docs/MAINAI_EXECUTION_LOOP_V0_1.md`, which this branch builds directly on top of —
V0.2 adds no new job queue, lease, or heartbeat system; every dead attempt V0.2 recovers is
still a real, leased, fenced `mainai_jobs` row (V0.1's own runtime, unchanged).

V0.1's own "V0.2 CANDIDATES" section named the gap this branch closes: *"Today's resume story
is 'the SAME work resumes once reclaimed' — real and tested, but assumes the reclaiming worker
runs the identical code. V0.2 should address: a worker that reclaims a job whose original
worker is provably dead ... taking deliberate salvage action."* V0.2's shape: **Detect
(dead `task_execution` job) → Inspect (durable evidence snapshot) → Classify (deterministic
A-I judgment) → [Approval gate, only for PUSHED_NO_PR/PR_EXISTS] → Salvage (copy real evidence
forward) → Takeover (fence the dead attempt, dispatch a genuinely new one)**.

## Why `task_execution` needed its own recovery path

V0.1's `claim_next_mainai_job()` already reclaims *any* `running` job with an expired lease,
same `job_id`, bumped `lease_generation` — correct for a job type like `corpus_review` (pure
reads, safely blind-resumable), but wrong for `task_execution`: the one job type with real,
semi-irreversible external side effects (local git commits, GitHub pushes). Migration 0034
excludes `task_execution` from that blind-reclaim branch entirely (`app/jobs/mainai_job_lease.py`'s
`_CLAIM_SQL`) and adds a `superseded` terminal status + `superseded_by_job_id` — a dead
`task_execution` job's honest outcome once `execute_takeover()` replaces it, never left sitting
at `running` forever with a dead lease (this codebase's standing "no fake/misleading status"
rule).

## Architecture at a glance

```
mainai_jobs row (task_execution, running, lease expired)
        │
        ▼  get_or_create_recovery_record()          MainAIRecoveryRecord (detected)
        ▼  inspect_recovery_record()                 evidence snapshot -> (inspected | blocked)
        ▼  classify_recovery_record()                 RecoveryClassification A-I -> classified
        ▼  require_recovery_approval()      [only for PUSHED_NO_PR / PR_EXISTS]
        ▼  execute_takeover()
             ├─ reset_task_for_takeover() + dispatch_ready_task()  -> genuinely NEW mainai_jobs row
             ├─ salvage_recovery_record()   -> copies real evidence/checkpoints/worktree forward
             └─ mark_job_superseded()       -> fences the OLD job, rejects any stale write
```

Every table (`mainai_task_worktrees`, `mainai_recovery_records`, `mainai_recovery_events`) was
added in migration 0033; `mainai_jobs.status = 'superseded'` in migration 0034; the recovery
approval gate's new event type in migration 0035. See `app/models/mainai_recovery.py` for the
full column-level rationale.

## Module map

| Module | Responsibility |
|---|---|
| `app/mainai_execution/worktree.py` | Real per-`(task, job)` isolated git checkout, ownership verified via an on-disk marker token (never trusted by path/task_id alone) |
| `app/mainai_execution/recovery_inspector.py` | `get_or_create_recovery_record()` (detect), `inspect_recovery_record()` (durable evidence snapshot: checkpoints, worktree, remote branch/PR, verification outcome, applicable engineering lessons) |
| `app/mainai_execution/recovery_classifier.py` | `classify_recovery_record()` — deterministic, evidence-only A-I judgment |
| `app/mainai_execution/recovery_approval.py` | `RECOVERY_APPROVAL_POLICIES`, `require_recovery_approval()` (the enforcement point), `grant_recovery_approval()` |
| `app/mainai_execution/recovery_salvage.py` | `salvage_recovery_record()` — copies durable evidence (checkpoints, worktree, verification) forward to the new attempt; re-verifies the remote branch tip live before trusting stale inspection-time evidence |
| `app/mainai_execution/recovery_takeover.py` | `execute_takeover()` — the orchestration entry point: approval gate, task reset, fresh dispatch, salvage, fencing the dead job |
| `app/jobs/mainai_job_lease.py` | `mark_job_superseded()` (row-locked, re-checks the job is still genuinely dead before writing) |
| `app/mainai_execution/final_report.py` | `_recovery_history()` — recovery attempts surfaced per-task and in the goal summary |
| `app/mainai_execution/recovery_inspector.py` | `applicable_lessons` evidence — read-only `lookup_lessons()` reuse, never auto-recorded |
| `app/routers/mainai_execution.py` | Three new founder-only endpoints (see MINIMAL API below) |

## REAL

- Real per-attempt filesystem isolation (`worktree.py`), **wired into the actual production
  `repo_edit` execution path, not just exercised by the recovery pipeline's own tests.**
  Hardening-pass Round 2 finding and fix: `execution_job.py`'s `_handle_repo_edit()` now
  creates (or, on a takeover's rebound worktree, reuses) a genuine `git` checkout on a
  deterministic task-scoped branch, created from a GitHub-verified base SHA — never the shared
  worker checkout, never `main`/`master`/the mainline branch (enforced both in code and by a DB
  CHECK constraint) — for every real repo_edit attempt when `github_write_enabled` is on.
  `_finalize_repo_edit()` pushes that same real local commit via `worktree.py`'s
  `push_worktree_branch()`, replacing the GitHub Git Data API-only push model V0.1 used. This
  means LOCAL_UNCOMMITTED_WORK and LOCAL_COMMITTED_NOT_PUSHED are genuinely reachable from a
  real dead `repo_edit` task today, proven end to end through `run_task_execution_job()` itself
  (Demo 2/3, see DEMO RESULTS) — not merely correct against hand-constructed evidence. The
  original V0.1 proposal-mode path (`github_write_enabled` off) is unchanged, preserved
  verbatim as `_propose_repo_edit()`.
- Real durability for every fact the recovery classifier depends on: the worktree row itself,
  and separately `worktree.current_commit` (the exact DB column
  `LOCAL_COMMITTED_NOT_PUSHED` reads), are committed to Postgres immediately after each real
  git-level fact they describe becomes true — each commit point gated behind the same
  lease-renewal check every other real write in `execution_job.py` already uses. Found and
  fixed in the same pass: without this, a genuine process crash (not a caught exception) would
  have rolled back the in-flight transaction along with real, already-on-disk git state,
  reproducing the exact "recovery state a real crash can't produce" problem one level deeper —
  see the recorded engineering lesson.
- Real, falsifiable ownership: a worktree is only ever trusted after
  `verify_worktree_ownership()` reads back an on-disk marker file and compares it field-for-field
  against the DB row — missing, unreadable, or mismatched is always treated as "nothing here
  belongs to this task", matching the founder's explicit fail-closed requirement.
- Real durable evidence snapshot, gathered once and never silently re-derived:
  `mainai_checkpoints` (was `work_result`/`finalized` ever durably recorded?),
  `mainai_task_worktrees` (does an owned checkout exist, is it clean, what's its local HEAD?),
  live GitHub reads (`get_ref`, `list_pull_requests_for_head` — never trusted from a stale local
  ref), `mainai_task_events` (did verification actually pass?), and applicable
  `engineering_lessons` (read-only context for a human reviewing a blocked record).
- Real ancestry check (`is_ancestor()`), not just a SHA-mismatch heuristic — tells apart the
  completely normal "haven't pushed yet" state from genuine remote divergence
  (CONFLICTED_STATE).
- Real deterministic classification: `_classify()` is a pure function over the evidence dict,
  first-match-wins, CONFLICTED_STATE checked first because a genuine divergence makes every
  other signal untrustworthy.
- Real approval gate as code (mirrors `approval.py`'s existing pattern exactly): PUSHED_NO_PR
  and PR_EXISTS require an explicit `approval_granted` `MainAIRecoveryEvent` before
  `execute_takeover()` proceeds past `require_recovery_approval()` — checked BEFORE any
  mutation, not after.
- Real salvage: durable checkpoints (`work_result`, `finalized`, and — V0.2 addition —
  `verification`) are copied forward to the new job_id rather than recomputed; a worktree with
  real uncommitted or committed content is REBOUND to the new job, never abandoned or recreated;
  the remote branch tip is re-verified live (`GitHubClient().get_ref()`) immediately before
  salvage trusts it, closing a TOCTOU gap between inspection time and takeover time.
- Real fencing of the dead attempt: `mark_job_superseded()` re-checks under a row lock that the
  job is STILL genuinely `running` with an expired lease before writing anything — a stale
  worker's own lease-renewal or checkpoint-write attempt after takeover is rejected by the exact
  same mechanism V0.1 already uses for any other fenced-out lease (`JobLeaseLostError` /
  `JobNotSupersedableError`), proven in Demo 6.
- Real duplicate-side-effect prevention: a salvaged `work_result`/`finalized` checkpoint means
  the new attempt never re-pushes a second commit or re-opens a second PR; a salvaged
  `verification` checkpoint (V0.2 addition — new checkpoint step, recorded only on PASS) means
  the new attempt never silently re-runs a real subprocess test suite it already durably passed.
- Real final-report integration: `_recovery_history()` surfaces every recovery attempt for a
  task (classification, status, blocker, manual_review_required) in both the per-task report and
  the goal-level summary counts; a task stuck behind a `blocked` recovery record now correctly
  reads as `unresolved_risk=True` (a genuine truthfulness gap found and fixed this branch — it
  previously read as no risk at all).
- Real, minimal, founder-only API (`GET /tasks/{id}/recovery`, `POST /tasks/{id}/recover`,
  `POST /recovery/{id}/approve`) that runs the ACTUAL pipeline through HTTP, never a shortcut
  that fakes the endpoint's own job.
- All 7 of the founder's required demo scenarios pass through the real recovery loop end to end
  — see DEMO RESULTS.

## STUBBED

Nothing in this list. Every piece named above does real work against real state.

## LIMITED

Named explicitly rather than silently left implicit:

1. **No worker automatically runs recovery.** `POST /tasks/{id}/recover` is the one entry
   point that actually runs detect → inspect → classify → takeover — mirroring V0.1's own
   `POST /goals/{id}/plan` precedent (the one addition beyond pure read/act). There is no
   background poll cycle that notices a dead `task_execution` job and recovers it on its own;
   a founder (or a future automated caller) must call it.
2. **Recovery approval is all-or-nothing per classification, not per-record policy
   selection.** `standard_recovery` is the only policy V0.2 ships (mirrors `approval.py`'s own
   "exactly one policy" V0.1 precedent) — there is no UI or API for defining a second named
   recovery-approval policy.
3. **CONFLICTED_STATE / UNSAFE_TO_AUTO_RECOVER have no in-app resolution workflow.** They
   correctly fail closed (`manual_review_required=True`, `execute_takeover()` refuses outright)
   but resolving the underlying git divergence happens entirely out-of-band (a human fixes it
   directly), then a fresh inspection pass — there is no "resolve and retry" button.
4. **`recover_task()` (the API endpoint) adds no additional liveness pre-check of its own.**
   Calling it on a task whose job is not actually dead is safe (every underlying primitive —
   `mark_job_superseded()`'s row-locked re-check, the classifier's own evidence-only judgment —
   already fails closed), but the endpoint does not itself verify the job is dead before
   starting; it would only ever duplicate a guard that already exists at the real point of
   consequence.
5. **Worktree cleanup on takeover is not exhaustive.** A rebound worktree keeps its real
   on-disk content; an abandoned one (e.g. NOTHING_DONE, where no worktree ever existed) is
   simply never created for the new attempt. There is no separate garbage-collection pass for
   worktree directories left behind by a container replacement.
6. **A narrow, structurally unavoidable crash window remains between a real git-level fact and
   its DB commit.** `_handle_repo_edit()`'s own commits happen IMMEDIATELY after each real git
   operation (worktree creation, local commit) — but "real git op, then commit the DB fact
   describing it" can never be a single atomic step across two different systems. A crash in
   that exact single-statement gap (vanishingly narrow, but not zero) still loses the DB-side
   fact even though the git-side fact survives; recovery would then under-classify that
   specific case (most likely as NOTHING_DONE for the worktree-creation gap, or
   LOCAL_UNCOMMITTED_WORK instead of LOCAL_COMMITTED_NOT_PUSHED for the commit gap) rather than
   over-claim safety. Not a new gap this branch introduces — the SAME class of gap exists for
   every other checkpoint commit in `execution_job.py`, V0.1 included; named here because the
   Round 2 durability fix makes it the narrowest it can practically be, not because it can be
   eliminated entirely.

## NOT IMPLEMENTED

- Any UI beyond the backend API (see the founder's own explicit "minimal API/UI" scope for this
  branch — no frontend page was added).
- Partial-progress salvage WITHIN a single non-idempotent step (e.g. resuming a `repo_edit`
  task's file write halfway through writing multiple files) — salvage operates at the
  checkpoint/git-state granularity V0.1 already checkpoints at, not sub-step granularity.
- Automatic engineering-lesson recording from a recovery outcome — `applicable_lessons` in
  recovery evidence is read-only lookup, never a new write path; respects V0.1's own explicit
  "lessons are never self-generated" precedent (`lessons.py`'s docstring).
- Force-push, destructive overwrite, or any recovery path that guesses past unverifiable state —
  deliberately excluded per the founder's explicit instruction ("V0.2 ska helst inte
  implementera force/destructive recovery alls. Fail closed istället.").

## KNOWN RISKS

- **No background poll cycle for recovery** (see LIMITED #1) means a dead `task_execution` job
  sits unrecovered until a founder (or a future caller) explicitly triggers `POST
  /tasks/{id}/recover`. At V0.2's scale this is a deliberate, named simplicity choice, not an
  oversight — see V0.3 CANDIDATES.
- **Worktree directories are not garbage-collected** (LIMITED #5) — a long-abandoned worktree
  whose recovery record never reaches a terminal state leaves real disk usage behind on
  whichever container/host created it, particularly relevant given the backend/worker
  filesystem is not persistent (`worktree.py`'s own module docstring).
- **The narrow git-op/DB-commit crash window** (LIMITED #6) — structurally unavoidable across
  two different systems, narrowed as far as practically possible by the Round 2 durability fix,
  not eliminated.
- **`lookup_lessons()` reuse inherits the same uncapped-result-set risk V0.1 already documented**
  for its own use inside `planner.py` — not newly introduced here, but now read on every
  `inspect_recovery_record()` call too.

## SECURITY INVARIANTS

- `mainai_task_worktrees`, `mainai_recovery_records`, `mainai_recovery_events` are all
  owner-scoped RLS (`FORCE ROW LEVEL SECURITY`), same convention as every other V0.1 execution
  table; `mainai_recovery_events` is append-only at the database level, same pattern as
  `mainai_task_events`.
- A working branch can never be `main`/`master`/the mainline branch — enforced both in
  `worktree.py` and by migration 0033's `ck_mainai_task_worktrees_branch_not_protected` CHECK
  constraint (defense in depth, not either/or).
- Worktree ownership is never trusted by path or task_id alone — `marker_token` (read back from
  an on-disk file, compared field-for-field against the DB row) is the actual trust anchor;
  missing/unreadable/mismatched always fails closed to "nothing here belongs to this task".
- `push_worktree_branch()` is always a plain, non-force push — origin either fast-forwards
  cleanly or git itself rejects it; a genuine divergence surfaces as an error for the classifier
  to see (CONFLICTED_STATE), never a silent overwrite. No code path in this branch ever passes
  `--force` to git.
- The V0.2 approval gate (`require_recovery_approval()`) is checked at the very top of
  `execute_takeover()`, before any mutation — an unapproved PUSHED_NO_PR/PR_EXISTS record cannot
  reach takeover, not "shouldn't", cannot; proven by
  `test_takeover_refuses_pushed_no_pr_without_founder_approval` /
  `..._refuses_pr_exists_without_founder_approval`.
- `mark_job_superseded()` re-verifies under a row lock (not merely re-reading a cached value)
  that the target job is still genuinely `running` with an expired lease before writing anything
  — a second, independent takeover attempt on an already-superseded job is refused
  (`JobNotSupersedableError`), proven in Demo 6.
- The API is founder-only (`require_founder`), same dependency every other MainAI execution
  router endpoint already uses; owner isolation for recovery records is enforced by Postgres RLS
  through the ordinary session, never re-derived in application code — proven by
  `test_recovery_api_a_founder_cannot_see_another_owners_recovery_records`.

## DURABILITY INVARIANTS

- A recovery record's `evidence` is a snapshot written exactly once by
  `inspect_recovery_record()` — never silently re-derived against evidence that has not itself
  changed; a genuinely NEW dead job for the same task always gets its OWN recovery record
  (`get_or_create_recovery_record()` is keyed by `job_id`, unique).
- `classify_recovery_record()` is idempotent: a record not yet `inspected` cannot be classified;
  a record already `classified` or later is returned unchanged rather than recomputed.
- Salvaged checkpoints are copied under the SAME `(task_id, new_job_id, step)` key convention
  V0.1 already established — never reused as if they belonged to the dead attempt's job_id.
- A `mainai_jobs` row can only reach `superseded` together with a non-null
  `superseded_by_job_id`, enforced in both directions by a migration 0034 CHECK constraint — one
  is never true without the other.
- `MainAIRecoveryEventType.approval_granted` (migration 0035) is recorded via
  `grant_recovery_approval()` ONLY from a genuine founder-driven action (a router call, or a
  test standing in for the founder's own click) — never from inside `execute_takeover()`'s own
  code path, which is what keeps the gate real rather than something the pipeline could grant
  to itself.

## CLASSIFICATION MODEL (the founder's A-I vocabulary)

Priority order, first match wins (`recovery_classifier.py`'s `_classify()`):

| Code | Evidence | Auto-salvageable? |
|---|---|---|
| `CONFLICTED_STATE` | remote branch tip is NOT an ancestor of the local worktree's HEAD — genuine divergence | No — `manual_review_required`, takeover refuses outright |
| `PR_EXISTS` | remote branch SHA matches local HEAD, and a PR already exists for it | Only with founder approval |
| `PUSHED_NO_PR` | remote branch SHA matches local HEAD, no PR yet | Only with founder approval |
| `LOCAL_COMMITTED_NOT_PUSHED` | a real local commit exists beyond the base SHA (`worktree_current_commit` set) | Yes |
| `LOCAL_UNCOMMITTED_WORK` | worktree ownership verified, real uncommitted changes on disk | Yes |
| `VERIFIED_WORK` | `verification_passed` event recorded, no git evidence beyond that (task types that never touch git) | Yes |
| `CHECKPOINTED_WORK` | a `work_result` checkpoint exists, nothing else | Yes |
| `NOTHING_DONE` | none of the above | Yes |
| `UNSAFE_TO_AUTO_RECOVER` | reserved for evidence that cannot be read cleanly (see below) | No — `manual_review_required` |

Anything `inspect_recovery_record()` itself cannot read cleanly (more than one worktree row
claiming the same job, a `git status` call failing, a PR lookup erroring) is recorded as an
`unsafe_reason` and the record is parked at `blocked`/`manual_review_required=True` at the
INSPECTION stage, before classification ever runs — never guessed past.

## APPROVAL MODEL (recovery-specific — distinct from task-content approval)

- `RECOVERY_APPROVAL_POLICIES` is a named registry, `standard_recovery` the only policy shipped
  — mirrors `approval.py`'s own "exactly one policy" precedent.
- Five of the seven auto-salvageable classifications need no approval at all (nothing has left
  this system's own local/durable state yet — purely internal bookkeeping); `PUSHED_NO_PR` and
  `PR_EXISTS` do, because the dead attempt's code is already visible on GitHub, a real external,
  shared-state artifact.
- An unknown policy name or classification fails CLOSED (requires approval) rather than guessing
  AUTO — same discipline `approval.py`'s `requires_approval()` already applies.
- This is a SEPARATE decision from V0.1's existing task-content approval gate
  (`require_task_approval()`), which still runs unchanged inside `execute_takeover()`'s own call
  to `dispatch_ready_task()` — recovery approval asks "is it safe to let an autonomous recovery
  pass take over a dead job's attempt", not "is this task's content allowed to run".

## RESTART / RESUME (duplicate side-effect prevention)

- A resumed job checks for an existing checkpoint before repeating a non-idempotent step — V0.1
  already proved this for `work_result`/the GitHub push; V0.2 extends the same discipline to
  **verification**: a new `"verification"` checkpoint step is recorded only on a PASS, and
  consulted before `verify_task()` is called again on resume — closing a real V0.1 gap where a
  resumed task silently re-ran real subprocess test verification a second time even when
  `work_result` itself was already durably checkpointed.
- Branch/commit salvage safety: `_verify_branch_unchanged_since_inspection()` makes a live
  `GitHubClient().get_ref()` call immediately before salvage trusts the remote branch SHA
  recorded at inspection time — closes a TOCTOU window where the remote could have changed
  between inspection and takeover.
- A stale worker that wakes up after its job has been superseded cannot renew its lease
  (`JobLeaseLostError`) and cannot itself supersede anything a second time
  (`JobNotSupersedableError`) — proven in Demo 6, the founder's explicitly REQUIRED stale-worker
  scenario.

## DEMO RESULTS

All 7 of the founder's required scenarios are real, automated tests (not manual walkthroughs),
run through the actual production recovery pipeline described above — see
`tests/backend/test_mainai_execution_recovery_demos.py`. Every demo dispatches a real task,
claims a real `mainai_jobs` row, does whatever real work (file write / commit / push / PR) the
dead worker is meant to have completed, THEN kills the lease (a worker dies after acting, never
before its claim) — never a manual shortcut that fakes evidence directly.

1. **Dead before any work** — `test_demo_1_dead_before_any_work_recovers_via_nothing_done`:
   NOTHING_DONE; the new attempt performs the real work for the first time (LLM call count
   asserted == 1); the dead job ends `superseded`. **PASSED.**
2. **Dead after a local edit** —
   `test_demo_2_dead_after_local_edit_recovers_via_local_uncommitted_work`: driven through the
   REAL `run_task_execution_job()`/`_handle_repo_edit()` path (Round 2 hardening pass — the
   original version of this demo called `worktree.py` directly and never exercised
   `execution_job.py` at all). `commit_worktree_changes()` is made to raise once so the real AI
   call and real file write happen for real before the simulated crash. LOCAL_UNCOMMITTED_WORK;
   the SAME worktree, with its real still-uncommitted content, is rebound to the new attempt and
   committed for real on resume — the AI is asserted called exactly once overall (no duplicated
   work). **PASSED.**
3. **Dead after a local commit** —
   `test_demo_3_dead_after_local_commit_recovers_via_local_committed_not_pushed`: same real-path
   principle — `push_worktree_branch()` is made to raise once so the real AI call, file write,
   AND local `git commit` all happen for real first. LOCAL_COMMITTED_NOT_PUSHED; the real local
   commit object (verified via `git rev-parse HEAD`, and via the durably-committed
   `worktree.current_commit` DB column) survives the takeover unchanged and is pushed for real
   on resume — asserted exactly once, never a redundant second commit. **PASSED.**
4. **Dead after a push** —
   `test_demo_4_dead_after_push_recovers_via_pushed_no_pr_with_founder_approval`: PUSHED_NO_PR;
   `execute_takeover()` raises `RecoveryApprovalRequiredError` until
   `grant_recovery_approval()` is called; once granted, the new attempt never re-pushes a
   second, redundant commit (remote branch log asserted unchanged). **PASSED.**
5. **Dead after a PR was opened** —
   `test_demo_5_dead_after_pr_recovers_via_pr_exists_with_founder_approval`: PR_EXISTS; also
   gated behind founder approval; the already-open PR is never duplicated (asserted still
   exactly one). **PASSED.**
6. **A stale worker returns AFTER takeover (REQUIRED)** —
   `test_demo_6_stale_worker_returns_after_takeover_and_is_rejected`: the dead worker's own
   lease-renewal attempt is rejected (`JobLeaseLostError`); a second independent takeover
   attempt on the same already-superseded job is also refused
   (`JobNotSupersedableError`); the dead job's row is untouched by the stale worker's return.
   **PASSED.**
7. **Ambiguous / genuinely conflicting state (REQUIRED)** —
   `test_demo_7_ambiguous_conflicting_state_fails_closed_no_destructive_recovery`: a real
   independent commit is pushed to the same branch from a different clone, creating genuine
   divergence; CONFLICTED_STATE, `manual_review_required=True`,
   `execute_takeover()` raises `TakeoverError` and refuses outright; the dead job stays
   `running` (never superseded), the task stays `running`, and the remote branch tip is
   verified byte-for-byte unchanged — nothing touched, nothing destroyed. **PASSED.**

## MINIMAL API

Three founder-only endpoints added to the existing V0.1 router (`app/routers/mainai_execution.py`),
matching its own "one entry point that actually runs the real pipeline" pattern:

| Endpoint | Behavior |
|---|---|
| `GET /tasks/{id}/recovery` | Lists recovery records for a task (read-only) |
| `POST /tasks/{id}/recover` | Runs detect → inspect → classify → takeover for the task's current `mainai_job_id`; returns the record either way — a caller inspects `classification`/`status`/`manual_review_required` to know what happened |
| `POST /recovery/{id}/approve` | Grants the recovery approval gate for one record; does not itself trigger a takeover — call `/recover` again afterwards |

Deliberately backend-only for V0.2 — no frontend page was added, per the founder's own "minimal
API/UI" scope for this branch.

## V0.3 CANDIDATES

Documented, not built, per the same "name it, don't build it yet" discipline V0.1's own
document used:

- **A background pass that notices dead `task_execution` jobs and calls the recovery pipeline
  automatically**, closing LIMITED #1 — today it is entirely founder/caller-triggered.
- **Worktree garbage collection** for abandoned worktrees whose recovery record never reaches a
  terminal state (LIMITED #5 / KNOWN RISKS).
- **An in-app resolution workflow for CONFLICTED_STATE**, so a founder can act on a flagged
  divergence without leaving the system (LIMITED #3).
- **A second named recovery-approval policy** if a future task type's external side effects need
  a different approval shape than `standard_recovery`'s all-or-nothing PUSHED_NO_PR/PR_EXISTS
  split (LIMITED #2).

## Coverage matrix

| Requirement | Covered by |
|---|---|
| Detect a dead `task_execution` job | `test_mainai_execution_recovery.py`, migration 0034 (`_CLAIM_SQL` exclusion) |
| Durable evidence snapshot | `test_mainai_execution_recovery.py` (inspector section) |
| Fail-closed on unreadable evidence | `test_inspect_blocks_with_manual_review_when_two_worktree_rows_claim_the_same_job` |
| Deterministic A-I classification | `test_mainai_execution_recovery.py` (classifier section, all 9 codes) |
| PUSHED_NO_PR/PR_EXISTS reachable with no worktree (hardening-pass P0 fix) | `test_classify_pushed_no_pr_is_reachable_with_no_worktree_at_all`, `test_classify_pr_exists_is_reachable_with_no_worktree_at_all` |
| CONFLICTED_STATE priority | Demo 7 |
| Worktree ownership trust (marker token) | `test_mainai_execution_worktree.py` |
| Salvage: checkpoints copied forward | `test_mainai_execution_recovery_salvage.py` |
| Salvage: live remote-tip re-verification (TOCTOU) | `test_salvage_refuses_when_remote_branch_tip_moved_since_inspection`, `..._deleted_since_inspection` |
| Takeover: fresh dispatch + fencing | `test_mainai_execution_recovery_takeover.py` |
| Stale worker rejected after takeover | Demo 6, `test_mainai_execution_recovery_takeover.py` |
| Duplicate-side-effect prevention (verification) | `test_takeover_salvages_verification_so_the_new_job_never_reverifies` |
| Duplicate-side-effect prevention (PR reuse) | `test_takeover_salvages_open_pr_work_result_so_the_new_job_never_recreates_the_pr`, Demo 5 |
| Approval gate enforced | `test_takeover_refuses_pushed_no_pr_without_founder_approval`, `..._refuses_pr_exists_without_founder_approval` |
| Approval gate satisfied | `test_takeover_proceeds_for_pushed_no_pr_once_founder_approval_is_granted` |
| Final report integration | `test_final_report_surfaces_recovery_history_for_a_recovered_task`, `..._flags_unresolved_risk_for_a_task_stuck_behind_a_blocked_recovery` |
| Engineering lesson reuse (read-only) | `test_inspect_surfaces_applicable_engineering_lessons_for_the_task_type`, `..._is_empty_when_none_apply` |
| Demo 1 (dead before work) | `test_demo_1_dead_before_any_work_recovers_via_nothing_done` |
| Demo 2 (dead after local edit) | `test_demo_2_dead_after_local_edit_recovers_via_local_uncommitted_work` |
| Demo 3 (dead after commit) | `test_demo_3_dead_after_local_commit_recovers_via_local_committed_not_pushed` |
| Demo 4 (dead after push) | `test_demo_4_dead_after_push_recovers_via_pushed_no_pr_with_founder_approval` |
| Demo 5 (dead after PR) | `test_demo_5_dead_after_pr_recovers_via_pr_exists_with_founder_approval` |
| Demo 6 (stale worker returns, REQUIRED) | `test_demo_6_stale_worker_returns_after_takeover_and_is_rejected` |
| Demo 7 (ambiguous state, REQUIRED) | `test_demo_7_ambiguous_conflicting_state_fails_closed_no_destructive_recovery` |
| API: auth/404/409 | `test_mainai_execution_recovery_api.py` (sections A/B) |
| API: real end-to-end recovery through HTTP | `test_recovery_api_recovers_a_nothing_done_task_end_to_end` |
| API: approve-then-recover two-step flow | `test_recovery_api_pushed_no_pr_requires_approve_endpoint_before_takeover_continues` |
| API: owner isolation | `test_recovery_api_a_founder_cannot_see_another_owners_recovery_records` |
| No force push, ever | `worktree.py`'s `push_worktree_branch()` — plain push only, no code path passes `--force` |
| Migration round-trip (0033-0035) | `test_migration_roundtrip.py` |
| Privilege policy narrows `mainai_recovery_events` (hardening-pass P1 fix) | `test_apply_mainai_execution_privileges_narrows_mainai_recovery_events_even_from_a_blanket_grant`, `test_mainai_recovery_events_mainai_app_lacks_update_delete_privilege_at_the_grant_level` |
| `worktree.py` wired into the real `repo_edit` execution path (hardening-pass Round 2 fix) | `test_mainai_execution_executor.py`'s `_handle_repo_edit`/`_finalize_repo_edit` real-worktree tests; Demo 2/3 |
| LOCAL_UNCOMMITTED_WORK/LOCAL_COMMITTED_NOT_PUSHED reachable from a REAL crash (not hand-constructed state) | Demo 2/3, rewritten Round 2 to crash inside `run_task_execution_job()` itself |
| Durability: worktree row + `current_commit` survive a real crash (hardening-pass Round 2 fix) | Demo 2/3 (assert durable state exists after `db_session.rollback()` simulates a dropped connection) |
| No duplicated AI call / commit / push on salvage-then-resume | Demo 2/3 (`call_count`/`push_calls` asserted) |
| Path/symlink-escape safety on the real worktree write path (re-attack, Round 2) | `test_run_task_execution_job_repo_edit_real_worktree_with_a_symlink_escape_never_writes_outside_the_worktree` |
| Ownership fail-closed on worktree reuse (re-attack, Round 2) | `test_handle_repo_edit_refuses_to_reuse_a_worktree_whose_ownership_does_not_verify` |
| Engineering lesson recorded for the state-not-reachable-from-production-path finding | `test_record_engineering_lesson_for_recovery_state_not_reachable_from_real_execution_path` |

## Test suite

New test files added by this branch:

- `tests/backend/test_mainai_execution_worktree.py` (13 tests)
- `tests/backend/test_mainai_execution_recovery.py` (19 tests)
- `tests/backend/test_mainai_execution_recovery_salvage.py` (8 tests)
- `tests/backend/test_mainai_execution_recovery_takeover.py` (13 tests)
- `tests/backend/test_mainai_execution_recovery_dedup.py` (4 tests)
- `tests/backend/test_mainai_execution_recovery_demos.py` (7 tests — the required demos, demos 2
  and 3 rewritten in the Round 2 hardening pass to run through the real `execution_job.py` path)
- `tests/backend/test_mainai_execution_recovery_api.py` (8 tests)
- `tests/backend/test_mainai_execution_checkpoint_fencing.py` (2 tests)

Plus, in `tests/backend/test_mainai_execution_executor.py` (V0.1's own file, not new but
substantially extended by the Round 2 hardening pass's worktree-wiring work): the real-worktree
repo_edit path now has its own dedicated resume/idempotency and path/ownership security tests
(`test_finalize_repo_edit_resumes_correctly_when_the_branch_already_exists_from_a_prior_uncheckpointed_push`,
`test_run_task_execution_job_repo_edit_real_worktree_with_a_symlink_escape_never_writes_outside_the_worktree`,
`test_handle_repo_edit_refuses_to_reuse_a_worktree_whose_ownership_does_not_verify`), alongside
the pre-existing V0.1 proposal-mode path-traversal/symlink-escape suite, unchanged.

Run the full backend suite: `pytest tests/`. Last full run after the Round 2 hardening pass:
1261 passed, 1 skipped (the P2 capacity test, skipped by design), 1 failed
(`test_a_successful_write_stream_means_the_blob_existed_at_safe_publish_completion`, a
storage-layer race test — confirmed genuinely flaky by rerunning it 3x in isolation
immediately after, passing 2 of 3 times; pre-existing, unrelated to this branch's diff, which
never touches `app/storage/` or `tests/backend/storage/`). The mainai + migration round-trip
subset (`pytest tests/ -k mainai`): 357 passed. `test_migration_roundtrip.py`: 2 passed. The
full V0.1+V0.2 execution suite (`test_mainai_execution_executor.py`,
`test_mainai_execution_recovery.py`, `test_mainai_execution_worktree.py`,
`test_mainai_execution_recovery_demos.py`, `test_mainai_execution_demos.py`) run together: 96
passed, 0 failed.
