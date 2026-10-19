# STOP / failure-classification regression package: production acceptance criteria

- **Scope:** the two remaining #286 P1 boundaries (examination `42bf874`, local), plus the accepted credential isolation and its open P2.
- **Network and credentials:** network-free and credential-free. Real production code paths run under a hermetic git environment.
- **Baseline:** recorded against #286 `a255fa23bf29be624d3f24e6387f7ed25defce1e`.

## How the package works

| Module | What it drives |
|---|---|
| `test_a_failure_origins.py` | The real worker (`Worker().run_once()`) runs a planned goal (repo_edit → open_pr). One failure is injected **at its real origin**. All later ticks run, with retry backoff, leases and wait polls made due. |
| `test_b_local_retries.py` | The same real path, with genuinely local failures. |
| `test_c_running_sibling.py` | A real two-chain goal. A goal-level STOP is committed from another session while sibling B is mid-flight. |
| `test_d_credential_extra_env.py` | `isolated_git_env` against a canary credential helper, inside a sandbox that denies the host's real helpers (`run_canary.sh`). |

**Paid-call accounting:** `OpenAIProvider.chat` is replaced by a recorder, so every call is one would-be paid request: `planner`, `code_agent` or `other`. Nothing leaves the host.

**Where each failure is injected:**
- **Real `git push` errors:** pre-receive decline, non-fast-forward, unreachable push endpoint.
- **Real `_run_git` formatting of a scripted `git push` subprocess result:** timeout, HTTPS 403, DNS/network. These need network access to reproduce, so the stderr is the verbatim git text.
- **Real `push_worktree_branch` post-push check:** remote/local tip mismatch, and a `GitHubClientError` 404.
- **Real `create_task_worktree` activation check:** approved base moved.
- **The publication boundary (right before `push_worktree_branch`):** the seven verbatim error texts of Codex's #280 grant substrate. The transport's typed errors do not exist yet; see FO-6.

## Criteria

All **P1** criteria must pass before a candidate can be accepted for the first supervised cycle. **P2** criteria are hardening and must be fixed or explicitly waived.

### FO: external failure origins (P1). `test_a_failure_origins.py`, 19 cases

For **every** case in the catalog:

| ID | Requirement |
|---|---|
| FO-1 | The repo_edit task ends terminal `failed`, with `next_retry_at = NULL`. Never `retryable_failed`, `ready` or `running`, including a task left `running` by a crashed job. |
| FO-2 | **Zero additional paid calls:** `code_agent` calls ≤ the single first attempt (0 when the failure precedes the agent), and exactly 1 `planner` call (the original plan; no replan). |
| FO-3 | Exactly one execution job is ever dispatched for the task: no automatic retry, no recovery or takeover re-run. |
| FO-4 | The dependent `open_pr` is never dispatched. |
| FO-5 | **Classification by origin, not text:** `opaque_boundary_failure` (a `WorktreeError` with no recognisable words, raised at the publication boundary) behaves exactly like a recognised failure. |
| FO-6 | **Untyped boundary exceptions are contained:** `untyped_boundary_exception` (a plain `RuntimeError`, as raised by #280's `ActivationAuthorityError`) must not leave the task stuck `running`. It must reach FO-1. |

Catalog: `git_pre_receive_declined`, `git_non_fast_forward`, `git_remote_unreachable`, `git_push_timeout`, `git_https_403`, `git_network`, `post_push_remote_mismatch`, `github_get_ref_404`, `grant_expired`, `grant_consumed`, `grant_base_moved`, `grant_head_mismatch`, `grant_envelope_superseded`, `grant_receipt_stale`, `grant_version_mismatch`, `opaque_boundary_failure`, `untyped_boundary_exception`, plus `approved_base_moved` (activation mode).

**Required production design (what the criteria imply):**
- Failures are classified by **origin**. Every exception that crosses the publication boundary is wrapped in a typed external-publication error carrying a cause enum, and is fail-closed. This covers: worktree base binding in activation mode, `_finalize_repo_edit` (including the post-push remote verification), `push_worktree_branch`, and any grant, transport or remote-effect call.
- An unknown failure **at** the boundary is external by default.
- Message text may be logged and recorded, but must never be the gate.

### LR: local failures keep legitimate behaviour. `test_b_local_retries.py`

| ID | Prio | Requirement |
|---|---|---|
| LR-1 | P1 | A genuine local verification failure (the edited test fails) is automatically retried with a fresh code-agent attempt (≥ 2 attempts observed). Once exhausted it remains replan-eligible (`find_replan_trigger` returns it). |
| LR-2 | P1 | A local publication-scope refusal (an out-of-scope file written by the agent) is retried as local. |
| LR-3 | P2 | Model-controlled text cannot select the external class. A file the agent names, e.g. `…approved_base_sha_push_grant_remote_rejected.py`, refused locally must still be treated as local. Satisfied automatically by origin-based classification. |

### RS: running sibling at terminal STOP (P1). `test_c_running_sibling.py`

**Scenario:** chain A has completed. Sibling B is mid-flight: its paid code-agent call and local commit are done, verification and push are not. A goal-level first-cycle STOP is then committed from another session.

| ID | Requirement |
|---|---|
| RS-1 | **Final pre-push authority check:** B's branch never reaches the remote. Immediately before the push, the job re-reads goal STOP state in the database and refuses. |
| RS-2 | The STOP itself sets `cancel_requested` on every running sibling's job, so cooperative checkpoints stop it as early as possible. |
| RS-3 | No paid call of any kind after the STOP, and B never becomes `completed`. |

### CI: credential isolation via `extra_env`. `test_d_credential_extra_env.py` via `run_canary.sh`

| ID | Prio | Requirement |
|---|---|---|
| C0 | control | Without isolation the canary helper is invoked (proves the test is live) |
| C1–C3 | P1 (accepted at #286) | The isolated env never invokes an outer helper. `extra` cannot re-point `HOME`/`GIT_CONFIG_GLOBAL`/`XDG_CONFIG_HOME`. Inherited `GIT_CONFIG_COUNT`/`KEY`/`VALUE`/`PARAMETERS` injection is stripped. |
| C4 | P2 (open) | `extra` must not re-enable a credential helper through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`/`GIT_CONFIG_PARAMETERS`. Reject or strip those keys **after** merging `extra`. |
| C5 | P1 (accepted) | The matrix sanitizer redacts `password=`, `gho_`/`ghp_`/`github_pat_`, and no repo-tree matrix copy is written by default |

`run_canary.sh` runs under `sandbox-exec` with a profile denying reads of the real `~/.gitconfig`, `~/.git-credentials`, `~/.config/gh`, `~/.config/git`, the keychains and the system gitconfigs, and denying execution of `gh`, `git-credential-osxkeychain` and `security`. The only credential material is a random per-run canary.

## Baseline at #286 `a255fa2`

| Criteria | Pass | Fail |
|---|---|---|
| FO (19) | `git_pre_receive_declined`, `git_non_fast_forward`, `grant_expired`, `grant_consumed`, `approved_base_moved` | **14:** `git_remote_unreachable`, `git_push_timeout`, `git_https_403`, `git_network`, `post_push_remote_mismatch`, `github_get_ref_404`, 5× `grant_*`, `opaque_boundary_failure`, `untyped_boundary_exception`. The typical failing case gets 3 jobs, **5 paid code-agent calls and a paid replan**. The untyped exception leaves the task stuck `running`. |
| LR | LR-1, LR-2 | LR-3 (P2) |
| RS | — | RS-1 and RS-2: B pushed and `completed` after STOP; its job not cancel-requested. RS-3's paid-call half holds (no paid call after STOP). |
| CI | C0, C1, C2, C3, C5 | C4 (P2) |

Full output: `results_a255fa2.txt` (A–C) and `results_d_a255fa2.txt` (D).

## Running

```
# prerequisites: local Postgres :5433, the backend venv at ../../venv312, Redis
redis-server --port 6390 --save "" --daemonize yes
REDIS_URL=redis://127.0.0.1:6390/0 ./run.sh <candidate-worktree> -rA         # modules A-C
./run_canary.sh <candidate-worktree>                                         # module D (macOS sandbox-exec)
bash gen_real_git_errors.sh real_git_errors.json                             # regenerate real git stderr fixtures
```

- `run.sh` verifies the candidate worktree is unchanged afterwards.
- The package never executes the candidate's `test_credential_isolation.py`, never reads host credential helpers, and makes no network calls.
- **Compatibility:**
  - modules A and B work on any candidate in the #284 → #286 lineage;
  - module C needs `app.mainai_execution.first_cycle_stop.stop_first_cycle` (the trigger only);
  - module D needs `first_cycle_acceptance._git.isolated_git_env` and `_matrix`.
