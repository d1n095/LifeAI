# Staging composition check — #285 `f5d9bf14340b0436d7470b28f6026342aeeaf3e6`

- **Status: composition VERIFIED — NOT ACCEPTED FOR ACTIVATION.** This is a limited, read-only composition check, not a certification. No PASS/FREEZE was issued: exact-SHA backend CI was still running when checked.
- **Examiner:** Claude, read-only.
- **Local evidence only, not yet published.** GitHub access is unavailable after the incident token revocation. Restoring or bypassing authentication is out of scope.

## 1. Status of the inputs

| Input | SHA | Examination status |
|---|---|---|
| #263 GitHub client / pagination | `e33f494` | accepted earlier; ancestor of #285 |
| #276 Path A task authority | `09b87e9` | PASS / FREEZE (`8ddaaee`); ancestor |
| #281 activation readiness (Claude builder) | `abc88b4` | Cursor #283 PASS / FREEZE; ancestor |
| #282 FC-4 npm-audit policy | `1e3ee3f` | PASS / FREEZE (`353b31a`); ancestor |
| **#284 review receipts / STOP** | `9c978de` | **RETURN_TO_BUILDER (`e0dbc53`), three open P1s, present unchanged in #285** |
| #280 activation authority | `37bca08` | RETURN_TO_BUILDER (`353b31a`), five P1s; **excluded** from #285 |
| Track B (`da4d96b`) | — | **excluded**; stays off |
| #279 acceptance harness | `ceb416e` | superseded by #284's copy; not an ancestor |

**The #284 P1 findings, carried into #285 unchanged** (`backend/app` and `backend/tests/backend/first_cycle_acceptance` are byte-identical to `9c978de`):

| ID | Finding |
|---|---|
| P1-1 | **The STOP is task-scoped, not goal-scoped.** A stopped goal's status is `blocked`, which still counts as active, and the worker's dispatcher never checks goal status. Other `ready` tasks of a stopped goal therefore get new jobs after the first cycle has stopped. |
| P1-2 | **Paid-replan containment is decided by task type, not by failure cause.** These external failures surface inside `repo_edit`, so they are auto-retried with a fresh paid code-agent run and then paid-replanned: GitHub rejecting the push, the approved base moving, and (once Codex's transport lands) a missing or stale push grant. `should_suppress_paid_replan(evidence=…)` is never passed any evidence. |
| P1-3 | **The harness's credential probe leaks real credentials.** CRED-4 runs `git credential fill` with the host's real environment and global git config. It records the raw output, which included a live GitHub token on the examiner machine (since revoked), and writes the matrix unconditionally into `<repo>/docs/examiner/`. Because of `testpaths = tests`, a plain `pytest` collects it. |

## 2. Composition results

| # | Check | Result | How established |
|---|---|---|---|
| 1 | Ancestry of #263/#276/#281/#282/#284 | **HOLD** | `git merge-base --is-ancestor` (run). Merge `ec2e896` equals a recomputed `git merge-tree` of #284 + #282 (run). |
| 2 | #280 and Track B excluded | **HOLD** | ancestry (run) plus a tree listing (run). One Alembic head: `0089_activation_branch_protect`. |
| 3 | FC-4 policy and the 12-check contract survive | **HOLD** | `ci.yml` diff (inspected): exactly #282's step on #281's triggers and permissions. Contract names vs `ci.yml` job names (inspected). Tests below (run). |
| 4 | #284 P1s visible, not reported as fixed | **PARTIAL** | Staging docs (inspected) never claim them fixed, but they predate `e0dbc53` and never record them. Manifest and registry say "provisional pending Claude exam". `first_cycle_staging_go_nogo.json` says "Accepted … review-STOP composition holds" and lists only Codex gaps. |
| 5 | The #279 matrix still reports transport and grant failures | **HOLD (inspected only)** | The harness is identical to #284, where it was run on `9c978de` (54 PASS / 8 FAIL / 1 BLOCKED). All 8 strict xfails remain. `go_nogo.remaining_codex` lists them, with `go: false`. **Not run on #285**; see §3. |
| 6 | The harness cannot reach real credential helpers or disclose secrets | **FAIL (inspected only)** | `_credentials.probe_role()` passes `{**os.environ}` to `git credential fill`. The `-c credential.helper=` reset applies only to the separate `config --get-regexp` call. CRED-4 records `fill_stdout`. `_matrix.write_payload()` always writes into the repo tree. |

## 3. Tests: run vs inspected only

**Every run used a hermetic git configuration:** `HOME=<empty tmp>`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`. Postgres and Redis were local and network-free. The detached worktree was verified unchanged.

| Item | Status | Result |
|---|---|---|
| `frontend/scripts/check-npm-audit.test.js` (FC-4 policy) | **run** | 59 / 59 pass |
| `test_ci_workflow_contract.py`, `test_first_cycle_staging_composition.py`, `test_mainai_activation_base.py`, `test_mainai_ci_wait_exact_set.py` | **run** | 108 passed |
| Ancestry, merge-tree fidelity, tree listings, file diffs vs #282 and #284 | **run** (local git objects only) | as in §2 |
| `backend/tests/backend/first_cycle_acceptance/**` (#279/#284 harness) | **inspected only, deliberately not run** | contains the unsafe CRED-4 probe |
| CRED-1..5 credential tests | **not run** (forbidden) | — |
| Full backend suite on #285 | **not run** | relies on CI |
| Exact-SHA GitHub Actions CI `f5d9bf1` | **read** via unauthenticated public API | backend unit/integration `in_progress` ×2; aggregate not reported; npm audit, TS/ESLint, builds, E2E ×3, Alembic, RLS and account lifecycle `success` ×2 |

The one `git fetch` at the start of the check ran under the clone's normal git configuration. The repository is public, so no credential challenge is expected. After that: no further git network operations, no `gh` use, and no re-authentication.

## 4. Safety rule for the credential probe

The probe must not run under a normal `pytest` or the host's real git configuration until #284's P1-3 fix lands:
- the harness must not be collected by default (an explicit marker or opt-in, not `testpaths = tests`);
- each git probe needs a hermetic environment: `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, a temp `HOME`, and an empty `credential.helper` on `credential fill` itself;
- record only a boolean or hash of the probe outcome, never `fill_stdout`;
- no unconditional matrix write into the source tree.

Until then, nobody runs `pytest` from `backend/` on a developer or Founder machine with git credential helpers configured, unless `first_cycle_acceptance` is explicitly deselected and the git configuration is hermetic.

## 5. Remaining blockers (first-cycle GO = false)

| Owner | Blocker |
|---|---|
| Cursor (#284 successor) | P1-1 goal-scoped STOP; P1-2 cause-based containment of external publication failures; P1-3 hermetic, non-recording credential probe. Then an independent re-exam and a re-composed staging. |
| Cursor (#285 records) | Record #284 as RETURN_TO_BUILDER (`e0dbc53`) with a `remaining_284` list in the manifest, registry and go/no-go file. Remove "review-STOP composition holds". |
| Codex (#280 replacement) | The five #280 P1s: account erasure, red CI, unverified GO, grant branch binding vs the #281 CHECK, verification not commit-bound. A single non-forked 0089 (re-point or fold `0089_activation_branch_protect`). |
| Codex (transport) | Credential-free git transport and remote-effect grants (CRED-1/2/3, E2E-3, GRANT-PROD). Publication content checks TA-12 (history), TA-13 (symlink), PUB-2 (gitlink). |
| #281 lane | Remove the legacy `evaluate_check_runs` (CI-6, dead code). |
| CI | Both backend runs and the aggregate check terminal at the staging SHA before any PASS/FREEZE. |
| Founder / GitHub (cannot be verified here) | Ruleset on `mainai/integration-founder-supervised-v1`, the runtime GitHub App identity and permissions, the integration branch itself, live required-check identities, attestation material, the spend grant, and the reviewer identity/receipt-recording runbook. |
