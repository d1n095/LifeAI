# Independent examination — #286 (#284 P1 corrections)

- **Candidate:** PR #286 (draft), branch `cursor/first-cycle-review-stop-p1-91c1`.
- **Examined SHA:** `a255fa23bf29be624d3f24e6387f7ed25defce1e`. The remote head was confirmed through the unauthenticated public API and the commit was already in the local object store. No authenticated GitHub access, no credential helper.
- **Base:** #284 `9c978de`, plus `f786e4c` (fix) and `a255fa2` (tests and registry). 14 files, +927/−71, no migration.
- **Original examination:** `e0dbc53` (#284 RETURN_TO_BUILDER, P1-1/P1-2/P1-3).
- **Examiner:** Claude, read-only.
  - Detached worktree, verified unchanged.
  - Every git-touching run used a hermetic git environment.
  - No paid calls, GitHub mutation, merge, deploy or Recall.
- **Local evidence only.** Not published: GitHub authentication was revoked.

## Verdict: RETURN_TO_BUILDER

| P1 from #284 | Status at `a255fa2` |
|---|---|
| P1-1 goal-level STOP | **Largely closed, one residual (P1-1b).** Dispatch, retry, takeover, readiness, wait polling and replan all refuse a stopped goal, and siblings are parked. A sibling **already running** at STOP is not asked to stop and proceeds to its push. |
| P1-2 cause-based paid-replan suppression | **NOT closed.** The classifier is a regex allowlist of message texts. 11 of 16 realistic external push and grant failures, including **five of #280's own grant-denial messages** and real `git push` stderr, are still classified as local. They are auto-retried as a fresh job (a paid code-agent run) and paid-replanned once attempts are exhausted. |
| P1-3 credential safety | **Closed.** The harness git environment is hermetic, nothing raw is recorded, and nothing is written into the source tree. Proven with a canary helper inside a sandbox that blocks the real host helpers (§3). One latent P2 remains. |

Preserved: exact-SHA review receipts, negative/missing/stale-head blocking, STOP-before-merge, never merged.

## 1. Goal-level STOP

| Probe | Result |
|---|---|
| S1/S2: CI-green without and with an exact PASS receipt → task and goal `blocked`, `merged=false`; no job or planner call across every `Worker.run_once()` MainAI tick | HOLD |
| S7: an independent `ready` sibling at STOP | **HOLD (fixed).** Parked as `blocked` (`parked_sibling: true`); never dispatched. |
| S10: a `retryable_failed` sibling with a due retry | HOLD: parked, `next_retry_at=None`, no job |
| S11: Founder/manual `dispatch_ready_task` → `FirstCycleGoalStoppedError`; `retry_task` and `reset_task_for_takeover` → `TaskNotRetryableError`. `execute_takeover` and the worker recovery and wait ticks are also guarded (code). | HOLD |
| S12 (control): an ordinary `blocked` goal without a first-cycle STOP still dispatches | HOLD |
| S13: STOP survives an overwritten `goal.final_outcome`, because task-event evidence (newest 80 events) also counts | HOLD |
| Supervisor path: `eligible_authorized_goals` selects only `running`, non-`path_a` goals | HOLD (code) |
| S6: repeated resolution events | HOLD (no-op) |
| **S9: a sibling already `running` at STOP** | **GAP, P1-1b.** `park_remaining_goal_tasks` deliberately leaves running siblings alone and only clears `next_retry_at`. It does not set `cancel_requested` on the sibling's job. `run_task_execution_job` has no goal-STOP check before `_finalize_repo_edit`, so the sibling performs its **remote push after the goal's terminal STOP**. Its dependents are not promoted. **Fix:** set `cancel_requested` on the running siblings' jobs (the existing cooperative checkpoints at "before edit" and "before PR" then apply), and add a `goal_is_autonomous_terminal` check immediately before the push. |
| S8: Founder cancel of a STOP task | still refused (P2, unchanged from #284) |
| S5: a receipt recorded after CI-green is never evaluated | unchanged (P2) |

## 2. Paid-replan suppression

**What #286 does:**
- `_finalize_task_outcome(…, cause=exc)` calls `classify_publication_failure`.
- On a match it calls `fail_closed_external_publication`: `failed`, no retry, goal STOP.
- `find_replan_trigger` and `trigger_replan` now pass `latest_failure_evidence` and refuse stopped goals.

**The defect:** classification depends on matching the exception **text** against `_PUSH_CAUSE`, `_BASE_CAUSE` and `_GRANT_CAUSE`. `_finalize_repo_edit` re-wraps `WorktreePushRejected` as `TaskExecutionError(str(exc))`, so the type check on `WorktreePushRejected` never fires on the real path.

**N1: 16 external failures, first attempt.** Real git stderr comes from `gen_real_git_errors.sh` (local bare remotes, hermetic). The other strings are verbatim formats from `worktree.py`, `github_client.py` and #280.

| Failure | Classified | Consequence |
|---|---|---|
| git non-fast-forward (real) | external | HOLD |
| git pre-receive hook declined (real) | external | HOLD |
| approved base moved (`ActivationBaseConfigurationError` text) | external | HOLD |
| #280 "push grant expired" | external | HOLD |
| #280 "push grant was already consumed" | external | HOLD |
| **git remote unreachable** (real: "does not appear to be a git repository") | **local** | `retryable_failed`. N1b: the retry tick re-dispatches a **new job** (`running`), i.e. a fresh paid code-agent run. |
| **git HTTPS 403** ("Permission … denied … returned error: 403") | **local** | retry |
| **git network** ("Could not resolve host") | **local** | retry |
| **git push timeout** (`_run_git` "timed out after 120s") | **local** | retry |
| **post-push remote/local mismatch** (`push_worktree_branch`) | **local** | retry |
| **GitHub `get_ref` 404** (`GitHubClientError` format) | **local** | retry |
| **#280 "base branch moved after approval"** | **local** | retry |
| **#280 "authoritative worktree HEAD no longer matches the grant"** | **local** | retry. N1c: with attempts exhausted, **the paid planner is called**. |
| **#280 "execution envelope was superseded after grant creation"** | **local** | retry |
| **#280 "preflight receipt is stale"** | **local** | retry |
| **#280 "authority version mismatch"** | **local** | retry |

Other results:
- **HOLD:** CI failed, timed out or cancelled; negative review; cancelled job; restart and repeated ticks (P1, P2, S4). The original P3 strings: stale base, protected-branch reject, "grant missing or stale". P3b no longer auto-retries.
- **Local behaviour preserved:** N2, a genuine verification failure, still becomes `retryable_failed` and is replan-eligible once exhausted. N3, a local publish refusal, stays retryable.
- **N4 (P2, informational):** a **model-chosen filename** containing a classifier keyword (`approved_base_sha_utils.py` in a local "Refusing to publish …" message) turns a local refusal into a goal STOP. This is the safe direction (no spend) but a model-controlled denial of progress.

**Fix (P1-2b): classify by where the failure happened, not by message text.**
- Any exception raised at or after the publication boundary (`_finalize_repo_edit`, `push_worktree_branch`, base binding in worktree creation, and any grant or transport call) should be wrapped in a typed `ExternalPublicationError` carrying a cause enum.
- Unknown failures at that boundary must default to external: fail closed, no retry, no replan.
- Keep text matching only as a secondary signal, never as the only gate.

## 3. Credential safety

**Static review:**
- Every harness subprocess goes through `isolated_git_env`: `_git.git()`, `_credentials.probe_role()`, and the probe script's inner calls, which inherit it. That function forces:
  - a temp `HOME` and `XDG_CONFIG_HOME`;
  - `GIT_CONFIG_GLOBAL` set to a harness config with an empty `credential.helper`;
  - `GIT_CONFIG_SYSTEM=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`;
  - removal of tokens, askpass and `GIT_CONFIG_*` injection from the inherited env.
- Caller `extra` cannot override `HOME` or the `GIT_CONFIG_*` paths.
- Raw `fill` output is never retained (a boolean plus a SHA-256 digest only), and `sanitize_matrix_text` redacts `password=`, `authorization=` and `gho_`/`ghp_`/`ghu_`/`ghs_`/`github_pat_`.
- The repo-tree matrix copy needs `FIRST_CYCLE_ACCEPTANCE_WRITE_REPO_COPY=1`.
- The harness only inspects production git code; it never executes it.

**Dynamic check** (`run_canary.sh`, `run_harness_sandboxed.sh`). Following the earlier directive, **CRED-4 and `test_credential_isolation.py` were not executed.**
- **Sandbox:** a macOS `sandbox-exec` profile (`deny_host_creds.sb.template`) blocks reading the real `~/.gitconfig`, `~/.git-credentials`, `~/.config/gh`, `~/.config/git`, the keychains and the system gitconfigs, and blocks executing `gh`, `git-credential-osxkeychain` and `security`. Verified before use: `blocked-gitconfig`, `blocked-gh`.
- **Canary:** the outer environment carries a canary global git config whose helper prints `password=CANARY-<random>` and records each call.

| Probe | Result |
|---|---|
| C0 control: plain `git credential fill` in the outer env | the canary is invoked and leaks, proving the test is not vacuous |
| C1: `isolated_git_env(os.environ)` | HOLD: canary never invoked, secret absent |
| C2: `extra` tries to re-point `HOME`/`GIT_CONFIG_GLOBAL`/`XDG_CONFIG_HOME` at the outer config | HOLD |
| C3: `GIT_CONFIG_COUNT`/`KEY`/`VALUE` and `GIT_CONFIG_PARAMETERS` helper injection in the inherited env | HOLD (stripped) |
| **C4:** the same injection passed through **`extra`** | **GAP (P2, latent).** `extra` is applied after the stripping, so a caller can re-enable a named helper. No current caller passes `GIT_CONFIG_*` (CRED-4 passes only `GIT_TERMINAL_PROMPT`; CRED-5 passes tokens and askpass). **Fix:** reject `GIT_CONFIG_*` keys in `extra`. |
| C5: the sanitizer redacts canary, `gho_`, `ghp_` and `github_pat_`; `write_payload` writes no repo copy by default | HOLD |
| Full harness **excluding** `test_credential_isolation.py`, run as ordinary pytest inside the sandbox with the canary env | **51 passed, 5 xfailed. Canary invoked: no. Secret in matrix/log: 0. Secret in target tree: 0. Repo copy written: no.** |

## 4. Codex failures remain visible

Matrix `out/h286_on_286.json` (CRED file excluded): **54 PASS / 5 FAIL / 1 BLOCKED, go=false**.
- **Still failing or blocked:** E2E-3 (finalize lacks an exact-SHA push grant), GRANT-PROD BLOCKED, TA-12, TA-13, PUB-2, CI-6.
- **Statically confirmed:** CRED-1/2/3 remain `@REMAINING_OUT_OF_SCOPE` strict xfails in `test_credential_isolation.py`, so they are not converted to PASS.

## 5. Regression and CI

| Run | Result |
|---|---|
| Probe suite `test_review_stop_286_exam.py` (all #284 probes re-run, plus #286 probes) | 29 HOLD / 22 GAP (`results_a255fa2.txt`). The #286-relevant GAPs are N1 ×11, N1b, N1c, S9 and N4. The rest are the known #284 P2s (S5, S8, R2 ×3, R3, R5). |
| 49 relevant backend files (first-cycle review/STOP including #286's new tests, ci_wait, all Path A, replan, recovery/takeover, executor, worker, envelopes, verification registry, account erasure, migration roundtrip, provider spend, ci_workflow contract) | **677 passed, 0 failed** (hermetic git; `alembic` on PATH) |
| Task-authority probes (e2e_v1) / spend probes (path_a_v1) | 19 passed (TA-13 P2 known) / 18 passed |
| Exact-SHA CI `a255fa2` (public API) | **terminated:** backend unit/integration `success` ×2. All product jobs `success` ×2 except the inherited `Frontend — npm audit` (#282 is not in this lineage), so `All required checks passed` fails. |

Two of my #284 probes had preconditions encoding the old behaviour: S7 expected the sibling to stay `ready`, and P3b expected `retryable_failed`. Both were updated to the secure intent, and both now HOLD.

## 6. Required corrections

1. **P1-2b:** typed, boundary-based classification of publication, base, grant and transport failures. Unknown failures at the boundary default to external. N1, N1b and N1c must all pass.
2. **P1-1b:** cooperative-cancel running siblings at STOP, plus a goal-STOP check immediately before `_finalize_repo_edit`'s push (S9).
3. **P2:** reject `GIT_CONFIG_*` in `isolated_git_env(extra=…)` (C4).
4. **P2:** do not let model-controlled path text drive STOP classification (N4).
5. **P2, carried from #284:** S5, S8, R2, R3, R5 (pre-existing 0080 erasure), the draft default, CI-6.

**Overall first-cycle activation remains NO-GO:** Codex transport and grants (#280 replacement), TA-12/13/PUB-2, #282 integration, and the Founder/GitHub facts.

## Reproduce

```
git worktree add --detach ../wt286 a255fa23bf29be624d3f24e6387f7ed25defce1e
bash probes/fc286/gen_real_git_errors.sh probes/fc286/real_git_errors.json        # local bare repos, hermetic git
env HOME=$(mktemp -d) GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 REDIS_URL=redis://127.0.0.1:<port>/0 probes/fc286/run.sh ../wt286 -rA
probes/fc286/run_canary.sh ../wt286                      # sandboxed; canary helper only
REDIS_URL=... probes/fc286/run_harness_sandboxed.sh ../wt286 h286_on_286   # sandboxed; CRED file excluded
```
