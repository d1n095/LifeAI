# Independent examination — #289 (first-cycle ops readiness + remaining execution-layer P2 hardening)

## Verdict

- `ca60ba5` (final head): **PASS / FREEZE** for the #289 scope. Executable gate PASS; exact-SHA CI fully green.
- `e688244` (assigned SHA): **RETURN_TO_BUILDER**. Exact-SHA backend CI failed on its own stale staging-record tests, which `ca60ba5` fixes.
- **Not activation GO** (§6).


- **Candidate:** PR #289 (draft), branch `cursor/first-cycle-ops-readiness-p2-91c1`, child of frozen #288 `1ea41d7`.
- **SHAs examined:**

| SHA | Content | Verdict |
|---|---|---|
| `e688244e665377ceee96973e25eedfef38a6e8da` (assigned) | 3 commits on #288 (production `d773502`, records `ba1b362`, `e688244`) | **RETURN_TO_BUILDER** |
| `ca60ba54febb49e1c7585fb776811564d54e3d21` (final head after the move) | `e688244` + `ca60ba5` (records and test only: restore frozen input SHAs, keep go=false); **no production change** (`backend/app`, `alembic`, `scripts`, `frontend` and `.github` are byte-identical to `e688244`) | **PASS / FREEZE (for the #289 scope)** |

- **Head check:** confirmed through the unauthenticated public API at the start and again at the end.
- **Examiner:** Claude, read-only.
  - Hermetic git and synthetic credentials; credential suites run in a sandbox that denies the host's helpers.
  - No paid calls, merge, deploy or Recall.
  - **Local evidence only.**

## 1. `e688244`: RETURN_TO_BUILDER

| Evidence | Result |
|---|---|
| Exact-SHA CI | `Backend — unit/integration tests` **failure** (run 118117788052; the second run was `cancelled` by the next push), so `All required checks passed` **failure** |
| Local cause (deterministic, 3/3 isolated runs) | the candidate's own `test_first_cycle_staging_composition.py`: `test_manifest_records_exact_frozen_input_shas` (`KeyError 'pr_275_frozen_parent_of_282'`) and `test_go_nogo_stays_false_with_codex_gaps_visible` (`KeyError 'parent_285'`). `d773502`/`ba1b362` rewrote the manifest and go/no-go records without updating the test. |
| Other suites | identical to `ca60ba5` below (same production code) |

`test_waiting_provider_defer_parks_blocked_with_backoff_not_immediate_ready` also failed once in the full local run, under parallel probe load. It passed 3/3 in isolation, so it is a **timing flake, not a candidate defect**. It is recorded here and passed again in the `ca60ba5` full run.

## 2. Production changes (inspected; identical in both SHAs)

| Change | Assessment |
|---|---|
| `first_cycle_ops.py` (new, 501 lines): ops snapshot (blockers, authorized actions, costs, SHA audit through append-only `progress_updated` events, `go:false`, `merged:false`); isolated local git helpers; `run_founder_supervised_local_dry_run` (refuses `github_write_enabled` or activation mode; approval-required task; dispatch refused before Founder approval; local verification; STOP-before-merge) | Sound for its purpose. **Reachable only from its test and `scripts/mainai/first_cycle_local_dry_run.py`** (no router, no worker). Hardening notes, P2-a. |
| Founder cancel beats recovery: `job_has_founder_cancel`, `finalize_interrupted_job_after_founder_cancel`, `mark_job_cancelled_after_dead_founder_cancel` (dead-lease fence), worker auto-recovery branch, `execute_takeover` refusal, `retry_task` refusal | Correct, and proven end to end (§3 FC) |
| Retry on stopped goal: `park_retryable_task_on_stopped_goal` (worker retry tick + `_finalize_task_outcome`) | Correct. Closes a retry-scan loop on stopped goals. |
| STOP truth: `_mark_goal_stopped` converts a locally `completed` goal back to `blocked` for STOP_BEFORE_MERGE and never clobbers an existing STOP | Correct |
| Classification: `failure_class` alone no longer selects the external class (a typed `origin` from our own code is required); `reason=="timed_out"` text check removed | Correct. Removes a model-smuggleable path. |
| Agent input hardening: file paths with whitespace or control characters refused; commit messages sanitized (single line, no NUL/control, leading dash stripped) and passed via `git commit --cleanup=strip -F -` (stdin, never argv) | Correct. P2-b note. |
| `final_report`: a stopped goal's report returns early | OK |

## 3. Executable evidence (`first_cycle_acceptance_v1` cursor profile, full, on `ca60ba5`)

| Suite | Result |
|---|---|
| `stop_classification`: FO-1..FO-5 (18 real origins), LR-1/2, RS-0..3, **FO-6b**, **FC-1..3** | **23 pass**; known P2: FO-6 (bare RuntimeError, crash semantics), FO-7 (base fetch). FIXED_P2: LR-3. |
| **FC (new, `test_g_founder_cancel_recovery.py`):** Founder cancels mid-job through the real `executor.cancel_task`, the worker then crashes, leases expire, all ticks run | **#289: task `cancelled`, one job, no takeover, no paid call after cancel, retry refused. Control #288: task left `running`, so the probe discriminates.** |
| `credential_canary` C0–C5 | 6/6 |
| `candidate_harness` (sandboxed, CRED file excluded) | canary never invoked, no secret, no repo-tree copy; matrix only Codex/#281-lane rows |
| `review_stop` | 22 pass; known P2 S5/S8/R2/R3/R5; 22 SUPERSEDED (text-shaped DB probes; see #288 verdict) |
| `task_authority` / `spend` | 15 HOLD (TA-12/13 known) / 19 HOLD |
| `backend_regression` (candidate domains incl. `test_first_cycle_ops_readiness_p2.py`, staging composition) | **763 passed, 0 failed** |
| FC-4 | 59/59; bypass known P2 B1/B2/B3/B7/B8 |
| Migrations | unchanged; single head `0089_activation_branch_protect` |
| Exact-SHA CI `ca60ba5` | **all 12 required checks + aggregate `success` in push and pull_request suites** (backend unit/integration completed 16:29–16:32 UTC; aggregate 16:33 UTC) |

## 4. Records

- `first_cycle_staging_go_nogo.json`: `go:false`. The reason now states "Do not treat #276, #287, or #288 as a GO certification", which **removes** the earlier "#286 P1 composition holds" claim (P2 from #287 closed).
- The manifest marks #276 and #275 roles "Not a first-cycle GO certification".
- **Residual P2:** the #284 and #286 rows still do not state their RETURN_TO_BUILDER verdicts.

## 5. P2 findings

| ID | Finding |
|---|---|
| P2-a | `run_founder_supervised_local_dry_run` rebinds module-global `execution_job._repo_root` for the duration of an async run, and claims "the next" job with `claim_next_mainai_job` rather than its own job. Safe only as the documented local, isolated-DB operator dry run; it must never be exposed through an API or run inside a worker process or against a shared database. Recommend a guard (refuse when other queued jobs exist; pass the root explicitly). |
| P2-b | `--cleanup=strip` turns a model commit subject beginning with `#` into an empty message, so git aborts the commit. That is a local failure (legitimately retryable, not an external-classification bypass). Prefer `--cleanup=verbatim` or reject a leading `#` in `sanitize_git_commit_message`. |
| P2-c | FO-7 (from #288): base-fetch network failure leads to bounded paid replans. Still open. |
| P2-d | FO-6 (since #284): a bare RuntimeError at push is crash semantics. Contained (FO-6b); auto-recovery logs an error every tick. |
| P2-e | Records: #284 and #286 RETURN_TO_BUILDER not recorded in the manifest or registry rows |
| P2-f | Carried: S5, S8, R2, R3, R5 (0080 erasure), FC-4 B1/B2/B3/B7/B8, TA-12/TA-13/PUB-2, CI-6 |
| P2-g | Lock hold during push and type-only failure evidence (from #288) |

## 6. Blockers for first-cycle GO (unchanged; GO = false)

1. Codex transport and grants (CRED-1/2/3, E2E-3, GRANT-PROD), with typed grant errors (`GrantDeniedError`) per the #288 contract.
2. #280 replacement and a single non-forked 0089.
3. TA-12/TA-13/PUB-2.
4. CI-6.
5. Founder/GitHub class-F facts (MATRIX.md).

## Reproduce

```
git worktree add --detach ../wt289b ca60ba54febb49e1c7585fb776811564d54e3d21
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wt289b
# e688244 failure: (hermetic env) pytest tests/backend/test_first_cycle_staging_composition.py  in a worktree at e688244
```
