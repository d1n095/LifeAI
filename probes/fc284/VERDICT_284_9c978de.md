# Independent security examination — #284 review receipts, CI-failure handling, STOP-before-merge

- **Candidate:** PR #284 (draft), branch `cursor/first-cycle-review-stop-91c1`.
- **Examined SHA:** `9c978ded05b05ad174ce003a3efad22682ec187d`. The remote head was re-verified at the start and again before publishing, and it did not move.
- **Commits on top of the frozen PR base #281 `abc88b4f479489c8c85111d754756058ab77eb22`:**
  - `55e7eea` production;
  - `bf8fe02` harness;
  - `9c978de` strict xfails.
- **Ancestry:** #276 `09b87e9`, #263 `e33f494`, #272 `c56e075` and #281 `abc88b4` are all ancestors.
- **Size:** 27 files, +4086/−34, no migration.
- **Examiner:** Claude, read-only.
  - Detached worktree, verified unchanged after every run.
  - No paid provider, GitHub mutation, merge, deploy or Recall.
- **Independence caveat:** #281, the parent, is my own builder work and was independently PASS-reviewed by Cursor (#283). I examine only what #284 adds.

## Verdict: RETURN_TO_BUILDER — 3 P1, 8 P2

**What holds:**
- The review receipt itself is well built:
  - the runtime role cannot write one;
  - it binds repo, PR, head, base, instruction, envelope and reviewer;
  - negative, inconclusive, missing and stale-head outcomes all block;
  - it never grants authority.
- CI-failure replan containment works for the `open_pr` task.

**Why it cannot be frozen:**
- **P1-1:** the STOP is **task-scoped, not goal-scoped**. After the first cycle stops, the worker still dispatches other `ready` tasks of the same goal.
- **P1-2:** paid-replan containment is keyed on **task type, not failure cause**. External publication outcomes that surface inside `repo_edit`, the task that performs the push, are treated as local failures:
  - GitHub rejects the push;
  - the approved base has moved;
  - after Codex's transport lands, a missing or stale push grant.

  They produce automatic re-dispatch (a fresh paid code-agent run per attempt) and then a paid replan.

**P1-3 (found during this examination's own secret scan):** running the backend tests on a machine with a GitHub credential helper writes the developer's **real GitHub token** into the acceptance matrix inside the repository working tree.
- **Why a plain `pytest` triggers it:** `pytest.ini` has `testpaths = tests`, so `backend/tests/backend/first_cycle_acceptance` is collected.
- **The leaking test:** CRED-4 (`test_credential_isolation.py::test_credential_helper_cannot_supply_authority`). Its `probe_role("worker", …)` runs `git credential fill` while inheriting the host's global and system git config. On this examiner machine that config includes a GitHub CLI credential helper.
- **What it records:** `detail=f"fill={probe.fill_stdout!r}…"`, which here was `password=gho_…`, a real token.
- **Where it is written:**
  - `FIRST_CYCLE_ACCEPTANCE_MATRIX`, by default `/opt/cursor/artifacts/first_cycle_acceptance_matrix.json`;
  - **unconditionally** `<repo>/docs/examiner/first_cycle_acceptance_matrix.json`. In #284's lineage that file is untracked, so `git add -A` would commit it. In #279's lineage it is a tracked file.
- **Who is exposed:** exactly the Founder/developer machines where the first cycle will be operated.
- **Containment here:** nothing was pushed. The examiner's copies were redacted before publication, and the published matrices in `out/` are redacted. The token owner has been told to rotate it.
- **Fix:**
  - run every harness git probe with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, an isolated `HOME`, and an empty `credential.helper`;
  - never record raw `credential fill` output (record a boolean or a hash);
  - never write the matrix into the source tree by default.

## 1. Freeze and provenance

| Item | Result |
|---|---|
| Remote head | `9c978de`, unchanged at start and at publication |
| Ancestry | linear on `abc88b4`. Three commits, all by Cursor Agent. |
| Migration | none. Alembic head stays `0089_activation_branch_protect`, inherited from #281. |
| CI terminated? | **No, not fully.** See §7. |

## 2. Review authority and exact-commit receipt

| Requirement | Result | Evidence |
|---|---|---|
| Runtime (`mainai_app`) cannot create a receipt | **HOLD** | R1. Migration 0080 has RLS `WITH CHECK (false)` plus a trigger rejecting `session_user = 'mainai_app'`. Only a privileged (migration/superuser) connection can record a receipt. |
| No forgery via task text | **HOLD** | S1: the goal instruction contains "REVIEW PASS … merge allowed", there is no receipt, and the result is `independent_review_blocked`. The evaluator reads only `mainai_verification_records`. |
| Binds repo, PR, head, base, envelope, instruction and reviewer | **HOLD** | `candidate_id = github:<repo>#<pr>`, `candidate_sha = head`, `candidate_tree_identity = base`, scope `output_hash`, `instruction_hash`, envelope context hash. The publication identity comes from server-side wait and checkpoint rows. |
| Changed PR head invalidates review | **HOLD** | S3: a PASS receipt for the previous head gives `independent_review_blocked` |
| FAIL / BLOCKED / missing block progress | **HOLD** | S1 (missing), S4 (FAIL) and the candidate's own tests (BLOCKED, malformed SHA) |
| Review success creates no authority | **HOLD** | S2: an exact PASS gives `stop_before_merge`, `merged=false`, `awaiting_founder_merge=true`, `subsequent_autonomous_job=false`. Task and goal stay `blocked`; across the full worker tick sequence no job is created and the planner is not called. The recorder hard-codes `reviewer_authority=[]` (R4). |
| Track B stays disabled | **HOLD** | T1: no Track B or reviewer-runtime flag is enabled, and #284 adds none |
| Reviewer ≠ builder | **P2** | R2: independence is plain string inequality against the constant builder identity `"path_a.builder"`. `"path_a.builder "`, `"PATH_A.BUILDER"` and `"path_a.builder​"` are all accepted as independent. Reviewer identity is only asserted (`identity_assurance="asserted"`). |
| Forged or replayed receipts | **P2** | R3: the evaluator takes the newest record by **caller-supplied** `reviewed_at`, so a PASS future-dated by a privileged writer shadows a later FAIL. Receipts with `owner_id IS NULL` are accepted for every owner. Both need the privileged insert path, so they are not reachable by the runtime. |

## 3. First-cycle terminal STOP

**Method.** The probes drive the real `resume_waiting_ci_task()` → `_stop_open_pr_after_ci()` / `fail_closed_external_ci()` path. Only the GitHub read (`poll_ci_wait`) is scripted. They then run every MainAI tick of `Worker.run_once()` in order:
- poll waits, retries, provider backoff, spend parks;
- auto-recovery, replan, goal finalize, supervisor, dispatch.

| Check | Result |
|---|---|
| PR opened / CI success / review PASS are each not merged | **HOLD** (S1, S2). `merged` is false everywhere. The client has no merge implementation, and PRs are created with `draft=True`. |
| A stopped task is never restarted (retry, recovery, provider wake, readiness recompute, repeated wait events) | **HOLD.** `recompute_task_readiness` touches only `pending`, and the retry tick only `retryable_failed`. Resolving the wait again is a no-op (S6). |
| No goal-close to `completed` after STOP | **HOLD** (S2: the goal stays `blocked`) |
| **No further autonomous job for the stopped goal** | **P1-1 GAP (S7).** See below. |
| Founder can still cancel a stopped task | **P2 GAP (S8).** #284 adds `cancel_task` → `TaskNotCancellableError` for first-cycle STOP tasks. The stopped task, and with it the goal, then has no API path to cancellation or closure. |
| Review evaluated when a receipt arrives | **P2 GAP (S5).** The review is evaluated **once**, at the moment CI turns green. A receipt recorded afterwards (the normal order) is never re-evaluated, so the recorded truth stays `independent_review_blocked` / `awaiting_founder_merge=false`. This fails closed but is misleading. |

### P1-1 — STOP does not stop the goal

- **What happens:** `stop_first_cycle()` blocks the open_pr task and sets the goal to `MainAIGoalStatus.blocked`.
- **Why it doesn't stop the goal:**
  - `blocked` is a member of `ACTIVE_MAINAI_GOAL_STATUSES` (`app/models/mainai_execution.py:41-43`).
  - `Worker._advance_mainai_execution_tasks()` selects `ready` tasks by envelope only and never checks goal status.
  - `fail_closed_external_ci()` does not touch the goal at all.
- **S7 reproduction:** a plan of repo_edit → open_pr plus an independent repo_edit T2. After CI-green, the STOP leaves T2 `ready`. The next worker cycle dispatches it (`status=running`, a fresh `mainai_job`). That job writes, pushes and can open a second PR after the first cycle "stopped".
- **Fix:**
  - Make STOP goal-scoped: a durable goal-level first-cycle terminal state that the dispatch, retry, replan and supervisor ticks all exclude.
  - Or cancel or park every non-terminal sibling task in the same transaction.
  - Do the same for `fail_closed_external_ci`.

## 4. Paid-replan containment

| Outcome | Result |
|---|---|
| Inherited npm-audit / failed required CI (`wait failed`) | **HOLD.** open_pr `failed`, `next_retry_at=None`; replan tick does not call the planner; no new job (P1[failed]). |
| Pending or missing checks → timeout | **HOLD** (P1[timed_out]) |
| Cancelled wait / cancelled job | **HOLD** (P1[cancelled], P2) |
| Negative review | **HOLD** (S4) |
| Stale PR head | **HOLD.** #281's ci_wait records `failed` on a moved head, and that is suppressed. |
| Restart / repeated ticks / repeated status events | **HOLD** (two full tick sequences; S6) |
| Original #279 failure (`find_replan_trigger` on a failed open_pr) | **CLOSED** (SPEND-6 PASS) |
| **External publication failure inside `repo_edit`** | **P1-2 GAP (P3 ×3, P3b)** |
| Control: a genuine local verification failure stays replan-eligible | **HOLD** (P4) |

### P1-2 — External publication outcomes in `repo_edit` reach automatic paid retry and paid replan

- **Where the push happens:** inside `repo_edit`, in `_finalize_repo_edit` → `push_worktree_branch`.
- **Where the approved-base check happens:** in worktree creation, where #281 raises `ActivationBaseConfigurationError(WorktreeError)` on a moved integration tip.
- **How they are recorded:** both reach the job's `except (… GitHubClientError, WorktreeError …)` block, which calls `_finalize_task_outcome(passed=False, evidence={"error": str(exc)})`.
- **How suppression decides:** `should_suppress_paid_replan()` returns False for every `repo_edit`. Its `evidence` parameter is **never passed** by either production caller (`find_replan_trigger`, `trigger_replan`), so the first-cycle state cannot reach the decision.
- **P3** drives that exact call with attempts exhausted: stale base, `[remote rejected] … protected branch hook declined`, push grant missing. In each case the replan tick calls the paid planner (`propose_plan_via_ai` → `billed_execution_chat`).
- **P3b**, the same failure before exhaustion: `retryable_failed`, then the retry tick and dispatch create a **new job**. That job has no `work_result` checkpoint, so it **re-runs the paid code agent** before failing at the push again.
- **Spec 8a5bdaf:** "Any branch movement … must not trigger a provider call or paid replan."
- **Composition:** this will also be the path for Codex's push-grant denials, which will fail inside `repo_edit` (§6).
- **Fix:**
  - Classify by cause, not type. Publication, base and grant failures raised at or after `_finalize_repo_edit` / worktree base binding should go through a `fail_closed_external_publication` with a first-cycle state: no retry, no replan, goal-level STOP.
  - Pass the latest failure evidence into `should_suppress_paid_replan` at both call sites.

## 5. #279 acceptance harness against #284

I ran the **original #279 harness (`ceb416e`, unmodified)** and **#284's extended copy** against `9c978de`, both outside the candidate tree. I also reviewed #284's harness diff: the only changed assertion is E2E-4 (a source-string check, as in #279). The rest is additive tests plus strict xfails, and no row was converted to PASS by a double.

| Row | #279 baseline (`fe4001e`) | #279 harness on #284 | #284 harness on #284 | Owner |
|---|---|---|---|---|
| SPEND-6 no paid replan after inherited CI failure | FAIL | **PASS** | **PASS** | #284 (closed for open_pr; see P1-2 for repo_edit) |
| E2E-4 STOP after review receipt | FAIL | **PASS** | **PASS** | #284 (string check; behaviour proven by S1–S4) |
| REV-5/6/7 evaluator present, missing review STOP, never merged | — | — | PASS | #284 |
| CI-6b `evaluate_required_check_runs` | FAIL | **PASS** | **PASS** | closed by #281 |
| CI-6 legacy `evaluate_check_runs` skipped-as-pass | FAIL | FAIL | FAIL (xfail) | dead code, no production caller. P2 cleanup in the #281 lane. |
| CRED-1/2/3 token in remote URL / `.git/config` / readable by subprocesses | FAIL | FAIL | FAIL (xfail) | Codex transport / credentials |
| E2E-3 finalize requires exact-SHA push grant | FAIL | FAIL | FAIL (xfail) | Codex grants (#280 RETURN_TO_BUILDER) |
| GRANT-PROD push/PR grant interface | BLOCKED | BLOCKED | BLOCKED | Codex |
| TA-12 history, TA-13 symlink, PUB-2 gitlink | FAIL | FAIL | FAIL (xfail) | publication content checks at push-grant time (Codex step, per my #276 verdict) |
| **Totals** | 51 PASS / 11 FAIL / 1 BLOCKED, go=false | 54 / 8 / 1, go=false | 57 / 8 / 1, go=false | |

- **Not covered by the matrix:** #279's SPEND-6 only exercises a failed `open_pr`, so the harness cannot see P1-2. P1-1 is likewise outside both harnesses.
- **Founder and GitHub facts (UNKNOWN in this process):** GitHub ruleset, App identity and token permissions, the integration branch existing, live required-check identities, real reviewer identity. #284 cannot prove these and does not claim to.

## 6. Composition and migration

| With | Result |
|---|---|
| #276 `09b87e9`, #263 `e33f494`, #281 `abc88b4` | ancestors. My task-authority probes on #284 give **19 passed** (TA-8b/8c/10a/10b/11′ HOLD; TA-13 the known P2 GAP). Spend probes SPEND-5/8/8a HOLD. Two legacy base probes fail identically on #276, #281 and #284: they plan `repo_edit` with no `target_files`, which is correctly refused. |
| #282 `1e3ee3f` | `git merge-tree` clean. No overlap. It would close the inherited `Frontend — npm audit` failure. |
| #280 `37bca08` (RETURN_TO_BUILDER) | Textual conflict in `backend/tests/backend/test_migration_roundtrip.py`. Two `0089` heads off `0088` (`0089_activation_branch_protect` inherited from #281 vs `0089_mainai_activation_security`). All #280 P1s stand, unresolved and not accepted here: erasure, CI, unverified GO, grant branch binding (a #281 CHECK forbids the worktree #280 requires), verification not commit-bound. Semantically, #280's future grant denials will surface inside `repo_edit` and hit P1-2. |
| Codex claim-action `ecf2f29` | the same `test_migration_roundtrip.py` conflict, plus a third `0089` child of `0088` |
| #279 `ceb416e` | add/add conflicts in 6 harness files, the harness doc and the registry. #284 supersedes #279's harness, so #279 should be closed as superseded, not merged. |

## 7. Regression and CI

| Run | Result |
|---|---|
| 48 relevant backend files: first-cycle review/STOP, ci_wait, Path A (all), replan, recovery/takeover, executor, worker, envelopes, verification registry, account erasure, migration roundtrip, provider spend | **656 passed, 1 failed.** The failure is `test_recovery_takeover_authority_fencing::test_downgrading_past_this_migration…`, with `FileNotFoundError: 'alembic'`: the test shells out to an `alembic` binary that is not on my local PATH. It fails identically on #281 `abc88b4` locally and passes in CI. Environmental. |
| #284 harness | 54 passed, 8 xfailed (strict) |
| Examiner probes `test_review_stop_284_exam.py` | **13 HOLD, 12 GAP** (`results_9c978de.txt`) |
| Exact-SHA CI `9c978de` | `Backend — unit/integration tests`: **one run success** (completed 06:52Z), **one run still `in_progress`** at publication, which is not counted as a pass. `Frontend — npm audit` fails ×2 (inherited; #282 not in lineage), so `All required checks passed` fails. Alembic, RLS, account lifecycle, E2E ×3, TypeScript, ESLint and builds are all `success` ×2. |

### P2 findings (summary)

| ID | Finding |
|---|---|
| P2-1 | S5: review evaluated only at CI-green; a later receipt is never evaluated, so `awaiting_founder_merge` is effectively never true |
| P2-2 | S8: a first-cycle STOP task cannot be cancelled by the Founder (no cleanup path; goal stays `blocked` forever) |
| P2-3 | R2: reviewer/builder independence is case-, whitespace- and Unicode-sensitive string inequality against a constant. Identities are asserted, not verified. |
| P2-4 | R3: the newest-receipt ordering trusts caller-supplied `reviewed_at`; global `owner_id IS NULL` receipts are valid for every owner |
| P2-5 | R5 (**pre-existing, 0080**): the `mainai_verification_records` trigger denies DELETE unconditionally while `owner_id` cascades from `users`, so account erasure fails once an owner-scoped receipt exists. #284 makes owner-scoped receipts a first-cycle artifact. Same class as #280 P1-1, and it needs the same `erase_own_*` fix. |
| P2-6 | There is no Founder-facing interface to record a receipt; only a privileged DB connection can. That is fail-closed and acceptable for the first cycle, but it must be part of the Founder runbook. |
| P2-7 | `_handle_open_pr` records `draft = pr.get("draft", True)`. A reused pre-existing PR with a missing field is reported as a draft. |
| P2-8 | Legacy `evaluate_check_runs` (skipped counts as pass) remains as dead code (CI-6) and should be removed in the #281 lane |

## 8. #284 readiness vs overall MainAI activation readiness

- **#284:** RETURN_TO_BUILDER for P1-1, P1-2 and P1-3. All three are small, local fixes inside #284's own boundary: a goal-scoped STOP, cause-based classification of publication failures, and a hermetic, non-recording credential probe.
- **Overall activation:** **NO-GO**, independent of #284:
  - Codex transport and grants (#280 RETURN_TO_BUILDER; CRED-1/2/3, E2E-3, GRANT-PROD);
  - TA-12/13/PUB-2 publication content checks;
  - the 0089 migration collision across #280, #281 and claim-action;
  - #282 not yet integrated (npm audit red on this lineage);
  - all Founder and GitHub external facts (ruleset, App, integration branch) unverified.

## Reproduce

```
git worktree add --detach ../wt284 9c978ded05b05ad174ce003a3efad22682ec187d
REDIS_URL=redis://127.0.0.1:<port>/0 probes/fc284/run.sh ../wt284 -rA
# #279 acceptance delta (run outside the candidate tree; the harness writes its matrix next to itself)
git -C ../wt284 archive ceb416e backend/tests/backend/first_cycle_acceptance | tar -x -C probes/fc284/h279 --strip-components=3
git -C ../wt284 archive HEAD    backend/tests/backend/first_cycle_acceptance | tar -x -C probes/fc284/h284 --strip-components=3
REDIS_URL=... probes/fc284/run_harness.sh h279 ../wt284 h279_on_284
REDIS_URL=... probes/fc284/run_harness.sh h284 ../wt284 h284_on_284
```

Matrices are in `out/`: `baseline_279_ceb416e.json`, `h279_on_284.json` and `h284_on_284.json`. The last two are **redacted**: the CRED-4 detail contained a real host token (P1-3).

**Warning:** do not run the harness on a machine with a global git credential helper until P1-3 is fixed. Otherwise run it with `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1`.
