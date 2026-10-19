# Independent examination — #288 (typed fail-closed publication errors, STOP cancel)

- **Candidate:** PR #288 (draft), branch `cursor/first-cycle-stop-classification-p1-91c1`.
- **Examined SHA:** `1ea41d7db424cba6b4d9ec93da79fa85d809d2fb`.
  - The remote head was confirmed through the unauthenticated public API.
  - The commit was already in the local object store; no authenticated access was used.
- **Lineage:** four commits on frozen staging #287 `1f1819a`: `14608a6`, `6b33dcb`, `021bd41`, `1ea41d7`.
  - Ancestors: #263, #276, #281, #282, #284, #285, #286 and #287.
  - Not included: #280 and Track B.
- **Examiner:** Claude, read-only.
  - Hermetic git and synthetic credentials for every run; credential suites run under a sandbox denying the host's helpers.
  - No paid calls, merge, deploy or Recall.
  - **Local evidence only.**

## Verdict: PASS / FREEZE (for the #288 scope) — both #286 P1s closed end to end; no new P1; 7 P2

The full `first_cycle_acceptance_v1` **cursor** profile gives **EXECUTABLE GATE: PASS** (0 blockers, 0 regressions). Exact-SHA CI is fully green.

This is **not** a first-cycle activation GO. Codex transport and grants, the #280 replacement, the publication-content checks and every Founder/GitHub fact remain open (§6).

## 1. P1-1b: running sibling cannot publish after STOP — CLOSED

**Production (inspected):**
- `park_remaining_goal_tasks` → `_request_cancel_running_sibling` sets `cancel_requested` on the running sibling's job, with an event, inside the STOP transaction.
- `_check_cancel_requested(task=…)` also raises `TaskCancelledCooperatively` once the goal is autonomous-terminal.
- `assert_publication_permitted` (`SELECT … FOR UPDATE` on the goal row) runs immediately before **and** after `push_worktree_branch` and around `open_pr`.
- `_finalize_task_outcome` turns a success on a stopped goal into a fail-closed `PublicationForbiddenAfterStop`.
- The worker's auto-recovery tick abandons running tasks of stopped goals (`abandon_running_task_after_stop`) instead of taking them over.

**Executable:**

| Probe | Result |
|---|---|
| RS-0..RS-3 (real worker, real git) | **PASS**. A STOP committed from a concurrent session at the start of B's verification commits promptly. B's branch never reaches the remote. B's job is cancel-requested. No paid call after STOP. B is not `completed`. |
| Control on #287 | RS-1 fails (B pushed and completed), so the probe discriminates |
| S9 (DB-level cancel request) | **PASS** |
| S7, S10–S13 (parking, dispatch/retry/takeover/readiness/replan guards, ordinary blocked goal unaffected, STOP survives `final_outcome` overwrite) | **PASS** |

**Probe note:** the original RS variant committed the STOP synchronously inside B's own thread. #288's STOP now also updates the running sibling's job row, so that variant self-deadlocked; that was a harness artifact. RS now triggers from a concurrent session at the start of verification, after B's `work_result` commit and before push, and a STOP blocked for more than 60 s is reported as RS-0 rather than hanging.

## 2. P1-2b: typed, origin-based failure classification — CLOSED

**Production (inspected):**
- **New module:** `publication_errors.py` defines `ExternalBoundaryError` with an origin and a STOP state (`PushRejectedError`, `GrantDeniedError`, `ApprovedBaseMovedError`, `GitHubApiBoundaryError`, `PublicationTimeoutError`, `PublicationNetworkError`, `PublicationUnknownError`, `PublicationForbiddenAfterStopError`, `ExternalCiContractError`).
- **`map_publication_boundary_error`:** maps by type and never reads the message. Unknown errors become `PublicationUnknownError` (fail closed).
- **`worktree.py`:** `WorktreePushRejected` and `ActivationBaseConfigurationError` are now `ExternalBoundaryError` subclasses. Every failed push becomes `WorktreePushRejected` without parsing stderr. A git timeout becomes `WorktreeTimeoutError`, which is mapped to a publication timeout at the boundary. `get_ref` failures and the post-push mismatch are typed `GitHubApiBoundaryError`.
- **`execution_job.py`:** around the push and the PR calls, `except Exception` is mapped to a typed error. Task evidence records only `type(exc).__name__` plus structured origin fields.
- **`first_cycle_stop.py`:** all regexes are removed. `classify_publication_failure` uses only `isinstance`, structured `origin`/`failure_class`, and our own flags.

**Executable (`stop_classification`, real origins):**

| Item | Result |
|---|---|
| FO-1..FO-5 | **18/18 required origins PASS**: terminal `failed`, zero extra paid calls, one job, `open_pr` never dispatched. Origins: real git pre-receive decline, non-fast-forward and unreachable endpoint; timeout; HTTPS 403; DNS; post-push mismatch; `get_ref` 404; seven #280 grant denials; opaque boundary failure; approved base moved. |
| Control on #287 | 5/19 |
| LR-1, LR-2 | **PASS**: local verification failure still retries and stays replan-eligible; local scope refusal retried |
| LR-3 (P2 at #286) | **FIXED**: model-chosen filename text can no longer force an external STOP |
| N4 (P2 at #286) | **FIXED** (same) |

**Superseded probes, explained rather than dropped:** the DB-level `review_stop` cases N1, N1b, N1c, P3 and P3b (22 rows) fail on #288 by design. They hand `_finalize_task_outcome` a `TaskExecutionError` whose **message text** describes an external failure. That is the #286 shape, which #288 intentionally ignores.

I checked that no production path still reports push, grant, base or GitHub failures that way:
- every remaining `TaskExecutionError(str(exc))` wrapper is on a local-only operation (envelope checks, local git inspection, pytest, ownership);
- worktree-creation errors propagate typed;
- the push is typed.

They are labelled `SUPERSEDED` in the summary, and their intent is gated by FO at the real origins.

## 3. Credential isolation, model-controlled classification, existing controls

| Area | Result |
|---|---|
| Credential canary C0..C5 (sandbox, canary helper) | 6/6. **C4 FIXED**: `isolated_git_env` strips `GIT_CONFIG`/`COUNT`/`KEY_n`/`VALUE_n`/`PARAMETERS` after merging `extra`. Production `_run_git`, fetch and ancestry calls also use `sanitized_git_subprocess_env()`. |
| Candidate harness (sandboxed; `test_credential_isolation.py` excluded) | canary never invoked; secret absent from outputs and target; no repo-tree copy. Matrix go=false with only the Codex/#281-lane rows (CI-6, E2E-3, GRANT-PROD, TA-12, TA-13, PUB-2). |
| Review receipts, negative review, STOP-before-merge, never merged (`review_stop` S1–S4, S6, R1, R4, T1) | PASS |
| Path A task authority (TA-8b/8c/10a/10b/11′) | 15 HOLD; TA-12/TA-13 known P2 (Codex publication step) |
| Spend (SPEND-*) | 19 HOLD |
| Backend regression (domains: Path A, ci_wait, replan, recovery/takeover, executor, worker, envelopes, verification, erasure, migration roundtrip, provider spend, staging, activation, github client, first-cycle) | **754 passed, 0 failed** |
| FC-4 | policy 59/59; bypass matrix 9 HOLD + known P2 B1/B2/B3/B7/B8 |
| Migrations | unchanged; single head `0089_activation_branch_protect` |
| Exact-SHA CI `1ea41d7` | all 12 required checks + aggregate `success` in push **and** pull_request suites |

## 4. P2 findings (none blocking the #288 scope)

| ID | Finding | Status |
|---|---|---|
| P2-1 (**new**, FO-7) | A network failure fetching the approved base during worktree creation (`git fetch --depth 1 origin <base>`) stays an untyped `WorktreeError`. It gets 3 free retries (before any code-agent call), then paid replans bounded by `MAX_AUTO_REPLANS = 3`: planner called 3 times in total (2 paid replans) against an expected 1. No code-agent spend, no publication. **Fix:** type remote fetch/clone failures in `create_task_worktree_sync` as `PublicationNetworkError`/`GitHubApiBoundaryError`. | open |
| P2-2 (FO-6, pre-existing since #284) | A *bare* `RuntimeError` at the push boundary is deliberately re-raised as crash semantics. It is contained (FO-6b: no paid call, no re-execution, Founder-visible recovery record `detected`) but not terminal. Auto-recovery logs "failed to auto-recover" on every tick, identically on #287. Codex's transport must raise typed errors (`GrantDeniedError` etc.); `ActivationAuthorityError` subclasses are already mapped to `PublicationUnknownError`. | open |
| P2-3 | `assert_publication_permitted` holds the goal row `FOR UPDATE` across the real push (`_run_git` timeout 120 s). A concurrent STOP or Founder write to the goal waits. The lock order (goal then job) is consistent with STOP, so no deadlock was observed. | observation |
| P2-4 | Task evidence for failures now records only the exception type name, which is right for classification, but the human-readable reason survives only in worker logs. Consider a separate, non-classifying `diagnostic` field. | observation |
| P2-5 (records, carried from #287) | `docs/first_cycle_staging_go_nogo.json` still says "#286 P1 composition holds". The #287 registry row still describes #286 as delivering the P1 fixes. #286's RETURN_TO_BUILDER is not recorded. (#288's own registry row is accurate.) | open |
| P2-6 | Carried from #284/#286: S5, S8, R2, R3, R5 (pre-existing 0080 erasure), the draft default | open |
| P2-7 | Carried: FC-4 B1/B2/B3/B7/B8; TA-12/TA-13/PUB-2 and CI-6 (Codex / #281 lane) | open |

## 5. Codex integration contract (carry into the `codex` profile)

1. Grant and transport denials must surface as typed `ExternalBoundaryError` subclasses (`GrantDeniedError`), or occur inside the push boundary. A grant check raising `TaskExecutionError` or plain `RuntimeError` outside the boundary would bypass this classification.
2. The FO grant cases currently inject at `execution_job.push_worktree_branch`. When Codex's transport adds its own grant call, the examiner adds an adapter at that **real** call site.
3. A single non-forked 0089 (re-point or fold `0089_activation_branch_protect`), plus the #280 P1s: erasure, red CI, unverified GO, grant branch binding, verification not commit-bound.

## 6. Overall first-cycle readiness (unchanged; GO = false)

- Codex transport and grants: CRED-1/2/3, E2E-3, GRANT-PROD.
- #280 replacement.
- TA-12/TA-13/PUB-2.
- CI-6.
- Founder/GitHub class-F facts (MATRIX.md): integration branch, ruleset, runtime App, attestation keys, spend grant, reviewer runbook, evidence publication method.

## Reproduce

```
git worktree add --detach ../wt288 1ea41d7db424cba6b4d9ec93da79fa85d809d2fb
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wt288
# individual follow-ups
probes/stop_classification_v1/run.sh ../wt288 -k "test_RS or FO7"     # (hermetic env: source first_cycle_acceptance_v1/hermetic_env.sh)
# FO-6b: pytest probes/stop_classification_v1/test_f_untyped_recovery.py (same env)
```
