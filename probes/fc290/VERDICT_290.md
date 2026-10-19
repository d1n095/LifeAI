# Independent examination — #290 (execution hardening P3: remote-fetch fail-closed, recovery-loop cap, publication locks)

- **Candidate:** PR #290 (draft), branch `cursor/first-cycle-execution-hardening-p3-91c1`, child of frozen #289 `ca60ba5`.
- **SHAs examined:**

| SHA | Content | Verdict |
|---|---|---|
| `c57c76820f126caa3f95b38294b7d353c500b487` | production `8733bc2` + records `3a8668a`, `c57c768` | not final. Exact-SHA CI **cancelled** (superseded by the next push), so `All required checks passed` = failure. Executable gate clean. |
| `6c988148a7e30531fac21e19c044f5570d95f501` (final head) | + `6c98814` (records/tests only; **no production change** vs `c57c768`) | **PASS / FREEZE (for the #290 delivered scope)** |

- **Head check:** through the unauthenticated public API at start and end.
- **Examiner:** Claude, read-only.
  - Hermetic git and synthetic credentials; credential suites sandboxed.
  - No paid calls, merge, deploy or Recall.
  - **Local evidence only.**

## Verdict

- `6c98814`: **PASS / FREEZE (for the #290 delivered scope)**.
- The delivered production changes are sound, there are no regressions, and the gate and CI are as recorded below.
- **Scope caveat (records P2):** #290 does **not** close the FO-6/FO-7 defects as I reported them. It hardens *adjacent* paths. Both defects remain P2, as before (§3).

## 1. Production changes (inspected)

| Change | Assessment |
|---|---|
| `worktree.fetch_remote_branch` (recovery/operator path): `ls-remote` then `fetch` via `_run_git` (sanitized env). A missing branch gives `None`; timeouts and other git failures become typed `PublicationTimeoutError`/`PublicationNetworkError`. FETCH_HEAD SHA is validated. | Correct |
| `recovery_inspector`: a GitHub 404 (or no status) means "branch absent". Any other `GitHubClientError`, network or timeout error sets `remote_fetch_failed` and is **never** mistaken for "no branch / no PR"; `pr_exists=None`. | Correct. Prevents a 5xx from masquerading as `LOCAL_COMMITTED_NOT_PUSHED` (an auto-salvage takeover with a paid re-run). |
| `recovery_classifier`: `remote_fetch_failed` gives `unsafe_to_auto_recover` | Correct (fail closed) |
| `first_cycle_stop.classify_publication_failure`: honours `remote_fetch_failed` / `remote_fetch_origin` set by our own inspector | Correct (structured, not text) |
| `recovery_takeover`: `MAX_AUTO_RECOVERY_TAKEOVERS_PER_TASK = 1` and `TakeoverCapExhaustedError`. The worker parks the task `blocked` with `manual_review_required` instead of a second takeover. The count uses `takeover_job_id IS NOT NULL`. | Correct. Bounds crash-recovery loops (each takeover = a paid re-run). |
| `assert_publication_permitted`: now `FOR UPDATE` on goal **then task** | Correct. The lock order differs from `stop_first_cycle` (task, then goal). A deadlock would need publication and STOP on the **same** task concurrently, which the state machine serializes (P2 observation). |
| `development_operator.push_branch`: a typed fetch failure becomes `OperatorAuthorityTransitionError` | Correct |
| `first_cycle_ops.founder_diagnostics` | Read-only, no secrets, `go:false` |

## 2. Executable evidence (`first_cycle_acceptance_v1` cursor profile, full, on `6c98814`; identical on `c57c768`)

| Suite | Result |
|---|---|
| `stop_classification` (FO 18 required origins, LR-1/2, RS-0..3, FO-6b, FC-1..3) | 23 pass; known P2 FO-6, FO-7; FIXED_P2 LR-3 |
| `credential_canary` | 6/6 |
| `candidate_harness` (sandboxed; CRED file excluded) | canary never invoked, no secret, no repo-tree copy; matrix only Codex/#281-lane rows |
| `review_stop` | 22 pass; known P2 S5/S8/R2/R3/R5; 22 superseded |
| `task_authority` / `spend` | 15 / 19 HOLD |
| `backend_regression` (incl. candidate `test_first_cycle_execution_hardening_p3.py`, staging, ops) | **775 passed, 0 failed** |
| FC-4 | 59/59 (+ known P2 B1/B2/B3/B7/B8) |
| Migrations | unchanged; single 0089 head |
| Exact-SHA CI `6c98814` | **all 12 required checks + aggregate `success` in push and pull_request suites** (backend completed 17:22–17:27 UTC) |

## 3. FO-6 / FO-7: what #290 does and does not close

| Reported defect (my probes) | #290 change | Status |
|---|---|---|
| **FO-7:** network failure on the **approved-base fetch during worktree creation** (`create_task_worktree_sync`: `git fetch --depth 1 origin <base>`, `worktree.py:342`). Untyped `WorktreeError` → 3 free retries → paid replans (planner ×3). | Not touched. #290 types the *recovery/operator* `fetch_remote_branch` instead. | **Still open (P2).** `test_FO7_…` fails identically on #290 (`jobs=3 planner=3`). |
| **FO-6:** a **bare `RuntimeError` at the push boundary** leaves the task `running` (crash semantics); contained (FO-6b) but not terminal | Not touched. #290 caps *repeated* takeovers, a different loop. | **Still open (P2)**, contained |
| (new, by #290) GitHub 5xx during recovery inspection treated as "no branch" leads to auto-salvage takeover (paid re-run) | fixed | closed (sound, per inspection; candidate tests) |
| (new, by #290) unbounded crash-recovery takeovers | cap = 1 | closed |

**Records P2:** `BRANCH_REGISTRY.md` (#290 row: "Claude remaining FO-7 remote-fetch paid-replan … FO-6 crash-recovery loop") and the go/no-go reason ("FO-6/FO-7 execution-layer completion") describe the Claude FO-6/FO-7 findings as completed. The probes above show they are not. The records should say "adjacent recovery-path hardening; FO-6/FO-7 (worktree-creation base fetch; bare RuntimeError at push) remain open".

## 4. P2 list (none blocking #290's scope)

1. FO-7 (worktree-creation base fetch): fix by typing failures of that `_run_git(["fetch", …])` (and the clone/remote setup) as `PublicationNetworkError`/`PublicationTimeoutError`.
2. FO-6 (bare RuntimeError at push): crash semantics; contained.
3. Records overstate FO-6/FO-7 closure (§3).
4. Publication/STOP lock-order asymmetry (goal→task vs task→goal), safe under per-task serialization.
5. Carried: the #289 P2s (dry-run global `_repo_root` / next-job claim; `#` commit subject with `--cleanup=strip`); S5, S8, R2, R3, R5; FC-4 B-items; TA-12/13/PUB-2; CI-6; #284/#286 RETURN_TO_BUILDER unrecorded.

## 5. First-cycle GO blockers (unchanged; GO = false)

1. Codex transport and grants (CRED-1/2/3, E2E-3, GRANT-PROD), with typed `GrantDeniedError`.
2. #280 replacement and a single non-forked 0089.
3. TA-12/TA-13/PUB-2.
4. CI-6.
5. Founder/GitHub class-F facts.

## Reproduce

```
git worktree add --detach ../wt290b 6c988148a7e30531fac21e19c044f5570d95f501
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wt290b
```
