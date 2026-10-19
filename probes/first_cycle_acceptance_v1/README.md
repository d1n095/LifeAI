# First-cycle independent acceptance package (v1)

This package consolidates the examiner's existing STOP/retry probes, credential-isolation tests and earlier security suites into one gated run. The final examination matrix is in `MATRIX.md`.

## Exact commands

**Prerequisites:**
- local Postgres on `:5433` (as used by every examination so far);
- Redis;
- the backend venv at `<examiner>/venv312`;
- Node, for the FC-4 tests;
- macOS `sandbox-exec`, for the credential suites.

```
redis-server --port 6390 --save "" --daemonize yes
git worktree add --detach ../wtCAND <exact-sha>          # never run against a moving branch

# Corrected Cursor staging successor (#287 successor)
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wtCAND

# Codex composed security candidate (transport + grants + #280 replacement on accepted staging)
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh codex ../wtCAND

# plumbing smoke test only (subset; never a verdict)
probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wtCAND --quick
```

**Outputs:** `out/<profile>-<sha7>/`
- one log per suite;
- `harness_safety.txt` and `ci_exact_sha.json`;
- `summary.json` and `summary.txt`, ending in `EXECUTABLE GATE: PASS|FAIL`.

The run also verifies that the candidate worktree is unchanged.

## Suites

Each suite feeds the executable gate (`summarize.py`).

| Suite | Source | Profiles |
|---|---|---|
| `stop_classification` | `../stop_classification_v1`: FO-1..6, LR-1..3, RS-1..3 (real worker, git push, retry/replan; paid calls counted at the provider stub) | both |
| `credential_canary` | `../stop_classification_v1/run_canary.sh`: C0..C5 (canary helper, sandbox denying host helpers) | both |
| `review_stop` | `../fc286`: receipts, STOP-before-merge, goal STOP, replan, account-erasure R5 | both |
| `candidate_harness` | the candidate's own `first_cycle_acceptance` harness, archived from HEAD, **excluding `test_credential_isolation.py`**, sandbox + canary; safety lines plus matrix rows | both |
| `task_authority` | `../e2e_v1` (TA-8b..TA-13) | both |
| `spend` | `../path_a_v1` (SPEND-*) | both |
| `authorization_grants` | `../activation_280`: Founder authority, preflight, grants, races, DB transitions, erasure E1/E1b, #281 shape | codex |
| `backend_regression` | the candidate's own backend suites for the affected domains | both |
| `fc4_policy` / `fc4_bypass` | candidate FC-4 unit tests, plus `../fc4_npm_audit_282` | both |
| `ci_exact_sha` | unauthenticated public API read of check runs at the exact SHA | both |

**Expectations** (`expectations/expectations.json`):
- `required` items failing are **BLOCKER**;
- `known_p2` items failing are listed but tolerated;
- anything else failing is a **REGRESSION**;
- a known P2 that now passes is reported as **FIXED_P2**.

## Safety guarantees (enforced by construction)

- **Synthetic credentials only:** the GitHub token is `"fake-not-used"`, the canary secret is random per run, and test tokens are `test-token-…` strings.
- **Hermetic git** (`hermetic_env.sh`): temporary `HOME`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, no token/askpass/agent/`GIT_CONFIG_*` variables, no prompts.
- **Credential suites** run under `sandbox-exec`. The profile denies reading the real `~/.gitconfig`, `~/.git-credentials`, `~/.config/gh`, `~/.config/git`, the keychains and the system gitconfig, and denies executing `gh`, `git-credential-osxkeychain` and `security`.
- **The candidate's `test_credential_isolation.py` is never executed.** CRED rows are judged from the candidate harness matrix only when the candidate claims them, and in that case only through the sandboxed runner, which still excludes that file. Codex's CRED evidence must come from the sandboxed C-suite plus static review.
- **Network:** git remotes are local bare repositories, and the GitHub REST layer and AI providers are in-process fakes. The only network access is one read-only, unauthenticated GET of public CI status.
- **Candidate trees are never written:** the harness is archived to a temporary directory, and every runner checks `git status` before and after.

## Adapter policy

The suites are written against the current production seams:
- `execution_job.push_worktree_branch` and `commit_worktree_changes`;
- `worktree._run_git` and its subprocess calls;
- `first_cycle_stop.stop_first_cycle`;
- `activation_security.*` (#280).

If a candidate renames or relocates one of these, for example when Codex's transport replaces `push_worktree_branch` with a remote-effect executor, the examiner adds a **thin adapter that injects at the new real origin**. Assertions are never weakened. An import failure is reported as `ADAPTER-NEEDED` (a blocker), never as a pass.

## Baseline

`baseline_run_287.txt` is a full `cursor` run at #287 `1f1819a`. It reproduces the two known P1s (SP-2/FO and ST-2/RS) and lists the known P2s. Use it to separate fixes from regressions in successors.
