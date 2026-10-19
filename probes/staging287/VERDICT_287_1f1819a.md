# Independent examination — staging composition #287 `1f1819aa46d8fd4aef18db5fb625af51c02d3394`

- **Candidate:** PR #287 (draft), `cursor/first-cycle-staging-p1-91c1`.
- **Composition:** #285 `f5d9bf1` + #286 `a255fa2`, no-ff merge `9c4e306`, plus 2 docs commits.
- **Remote head:** confirmed through the unauthenticated public API.
- **Examiner:** Claude, read-only. Detached worktree, verified unchanged.
- **Constraints:** hermetic git for every run. No credential helper, no authenticated GitHub access, no paid calls. **Local evidence only.**

## Verdict: RETURN_TO_BUILDER — composition correct, no new regression; two known #286 P1s present; one staging-record P2

- **The composition itself is faithful and fully green.** It introduces **no new security regression**: every executable outcome is identical to #286.
- **Not freezable:** #287 carries both open #286 P1s unchanged, and its own records describe them as fixed.

## 1. Integration correctness

| Check | Result |
|---|---|
| Ancestry | #263 `e33f494`, #276 `09b87e9`, #281 `abc88b4`, #282 `1e3ee3f`, #284 `9c978de`, #285 `f5d9bf1` and #286 `a255fa2` are all ancestors. #280 `37bca08`, Track B `da4d96b` and #279 `ceb416e` are not. |
| Merge fidelity | `git merge-tree` of #285 + #286 has no conflicts, and its tree equals merge `9c4e306` byte-for-byte |
| Production code | `backend/app`, `backend/alembic`, `backend/pytest.ini` and the acceptance harness are byte-identical to #286. `frontend/` is identical to #282. `.github/` is identical to #285. |
| Post-merge commits | docs and manifest only (`7aca75e`, `1f1819a`) |

## 2. Migration ancestry

- **Single head:** `0089_activation_branch_protect` (down `0088_erasure_completion_phase`). No #280 `0089_mainai_activation_security` and no claim-action 0089/0090.
- **Known future conflict, recorded in `FIRST_CYCLE_CODEX_INTEGRATION.md`:** Codex's accepted migration must not fork 0088. The `test_migration_roundtrip.py` conflict with #280 and claim-action is still pending.

## 3. CI contract

- **Exact-SHA CI `1f1819a`:** all 12 required checks plus the aggregate are `success` in **both** suites (push and pull_request, 2× each). That includes `Frontend — npm audit`, `Backend — unit/integration tests` and `All required checks passed`.
- **`ci.yml`:** #281's triggers and permissions plus #282's FC-4 steps (`audit:policy:test`, then `check-npm-audit.js`). `ACTIVATION_REQUIRED_CHECKS` (11 jobs + aggregate, app 15368, `.github/workflows/ci.yml`) matches the job names.
- **FC-4 policy tests:** 59/59.

## 4. Security-control preservation

| Control | Evidence | Result |
|---|---|---|
| Exact-SHA review receipts, negative/missing/stale-head blocking, STOP-before-merge, never merged | #284/#286 probe suite `test_review_stop_286_exam.py` on #287: **outcome set identical to #286** (29 HOLD; the same 22 known GAPs) | preserved |
| Goal-scoped STOP (parking; dispatch/retry/takeover/readiness/replan guards) | S7, S10–S13 HOLD | preserved |
| Credential isolation of the harness | sandboxed canary: C0, C1, C2, C3, C5 HOLD. Sandboxed harness run (CRED file excluded): 51 passed / 5 xfailed, **canary never invoked, secret absent, no repo-tree copy** | preserved |
| Codex rows remain visible | matrix 54 PASS / 5 FAIL / 1 BLOCKED, go=false: E2E-3, GRANT-PROD (BLOCKED), TA-12, TA-13, PUB-2, CI-6. CRED-1/2/3 remain strict xfail. | preserved |
| Regression: 51 backend files (Path A, ci_wait, replan, recovery/takeover, executor, worker, envelopes, verification registry, account erasure, migration roundtrip, provider spend, ci_workflow contract, staging composition, activation base, github client, first-cycle review/STOP) | hermetic git, `alembic` on PATH | **741 passed, 0 failed** |

## 5. Known unfixed P1s vs new staging regressions

**STOP/failure-classification package (`probes/stop_classification_v1`) on #287:** 7 pass / 15 fail, **identical** to the #286 baseline.

| Category | Item | Status at #287 |
|---|---|---|
| Known P1 (#286, `42bf874`) | **P1-2b: classification by message regex.** FO fails on 14/19 external failures: unreachable remote, timeout, HTTPS 403, network, post-push mismatch, `get_ref` 404, five of #280's grant denials, opaque boundary failure, untyped boundary exception. Typical case: 3 jobs, 5 paid code-agent calls and a paid replan. The untyped exception leaves the task stuck `running`. | **present, unchanged** |
| Known P1 (#286) | **P1-1b: running sibling at STOP.** RS-1 and RS-2 fail: the sibling pushes and completes after the goal STOP, and its job is not cancel-requested. No paid call after STOP. | **present, unchanged** |
| Known P2 (#286) | C4 `extra_env` helper injection; LR-3 model-controlled filename forces STOP; S5, S8, R2, R3, R5 (pre-existing 0080 erasure); draft default; CI-6 dead code | present, unchanged |
| **New staging regression, code** | none. All executable outcomes are identical to #286. | — |
| **New staging finding, records (P2)** | `first_cycle_staging_go_nogo.json` says "Accepted … / #286 P1 composition holds". `BRANCH_REGISTRY.md` describes #286 as "cause-based paid-replan suppression … → zero extra paid calls". The manifest calls #286 the "#284 P1 correction". None of them records #286's **RETURN_TO_BUILDER** or its two open P1s. This repeats, in a stronger form, the #285 record gap: the claim of zero extra paid calls is contradicted by FO above. `go: false` is correct, so this grants no authority, but it misstates certification status. | **new in #287** |

## 6. Blocker ledger (first-cycle GO = false)

| # | Blocker | Owner | Gate |
|---|---|---|---|
| 1 | P1-2b: origin-typed external-publication classification (unknown failure at the boundary = external), untyped boundary exceptions contained | Cursor (#286 successor) | `stop_classification_v1` FO-1..FO-6 all pass |
| 2 | P1-1b: STOP sets `cancel_requested` on running siblings, plus a final pre-push goal-STOP check | Cursor | RS-1..RS-3 pass |
| 3 | Staging records: mark #286 RETURN_TO_BUILDER (`42bf874`) with its open P1s in the manifest, go/no-go and registry; remove "zero extra paid calls" and "P1 composition holds" | Cursor (#287 successor) | records match examination status |
| 4 | P2s: C4 `extra_env` `GIT_CONFIG_*`; LR-3; S5/S8/R2/R3; R5 + #280 erasure path; draft default | Cursor / Codex | ACCEPTANCE.md P2 rows |
| 5 | #280 replacement (five P1s) and a single non-forked 0089 | Codex | independent PASS |
| 6 | Credential-free transport and grants (CRED-1/2/3, E2E-3, GRANT-PROD); TA-12/TA-13/PUB-2 publication checks; CI-6 removal | Codex / #281 lane | harness rows PASS without doubles |
| 7 | External facts: ruleset on the integration branch, the integration branch itself, runtime App identity and permissions, attestations, spend grant, reviewer runbook | Founder / GitHub | not verifiable here |

## Reproduce

```
git worktree add --detach ../wt287 1f1819aa46d8fd4aef18db5fb625af51c02d3394
REDIS_URL=redis://127.0.0.1:<port>/0 probes/stop_classification_v1/run.sh ../wt287 -rA
probes/stop_classification_v1/run_canary.sh ../wt287
env HOME=$(mktemp -d) GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 REDIS_URL=... probes/fc286/run.sh ../wt287 -rA
REDIS_URL=... probes/fc286/run_harness_sandboxed.sh ../wt287 h286_on_287
(cd ../wt287/frontend && node --test scripts/check-npm-audit.test.js)
```
