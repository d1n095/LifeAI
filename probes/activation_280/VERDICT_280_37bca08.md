# Independent security examination — Candidate A: #280 activation authority substrate

- **Candidate:** PR #280, branch `codex/mainai-activation-authority-substrate`.
- **Frozen head:** `37bca08a287f00b6282222047340bffa949ff788`. It is a single commit with 15 files (+1216).
- **PR base:** #272 `c56e0757cc117b443b377b987743ce2364558497`. The candidate does not contain #273 `e8914c6` or #276 `09b87e9`.
- **Spec it claims to implement:** Codex `8a5bdaf` (`docs/MAINAI_ACTIVATION_SECURITY_IMPLEMENTATION_SPEC.md`).
- **Examiner:** Claude, read-only.
  - I examined a detached worktree and verified it was unchanged after every run.
  - No live provider, GitHub mutation, merge, deploy or Recall.
  - This is an independent examination. I did not build #280.

## Verdict: RETURN_TO_BUILDER — 5 P1, 8 P2, plus 4 integration blockers

**What the substrate gets right:**
- Founder authority is correctly enforced at the HTTP layer.
- Consumption is a correct one-shot, race-safe operation.
- The DB triggers make terminal states and bindings immutable.

**Why it cannot be frozen:**
1. **Account erasure regression:** a single Founder preflight call, even one that returns NO-GO, makes `DELETE /api/account` fail permanently.
2. **Exact-SHA CI is red:** the backend tests fail because of this candidate.
3. **Unverified GO:** preflight returns **GO** on unverified, operator-typed attestation strings, and on an activation SHA that is a config value rather than the running code.
4. **Grant branch binding:** a grant can be minted only for a worktree that is checked out *on the integration branch*. Real Path A task worktrees therefore never qualify. Under #281 the database forbids such a worktree entirely.
5. **Verification is not bound to the commit:** a verification PASS for any commit authorizes a grant for a different commit.

## 1. Implemented security vs deferred transport

| Area | Status | Evidence |
|---|---|---|
| Founder-only HTTP authority | **Implemented — HOLD** | All three routes `Depends(require_founder)`, which checks role **and** the fixed `FOUNDER_USER_ID`. `founder_authorized=True` is a literal set in the router, never read from the request. Probes F1/F2/F3/G5. |
| Preflight facts from the server, not the caller | **Implemented — HOLD** | The router builds `AuthoritativePreflightFacts` from `Settings` and from a GitHub `get_ref` read. The JSON body cannot inject facts, digests, `observed_base_sha` or `founder_authorized`; they are ignored (F1). An unreadable `get_ref` gives NO-GO, not a 500. |
| Fact *quality* (attestations, activation SHA) | **Not implemented — silently treated as GO** | P1-3 |
| Immutable receipts and grant bindings | **Implemented — HOLD** | Triggers deny every UPDATE except one-way `invalidated_at` (receipts) or `active→terminal` (grants). Probe D1 covers 8 mutation attempts, all refused. Envelope activation bindings are immutable (D2). |
| One-shot consumption and races | **Implemented — HOLD** | `SELECT … FOR UPDATE` followed by `UPDATE … WHERE status='active'` with a rowcount check. Probe C1 uses two real sessions: B blocks on A's row lock, and after A commits B gets `PushGrantConsumed`. |
| Replay after cancellation or crash | **Implemented — HOLD** | An idempotent replay of a cancelled grant returns the cancelled row and cannot consume (G7). A consumed grant cannot be revived: the trigger and the candidate's own test both enforce it. |
| Owner isolation | **Implemented — HOLD** | FORCE RLS plus owner-composite foreign keys. Another owner cannot see or consume a grant (D3). |
| Grant-time re-reads (HEAD, remote task branch, spend, config) | **Partially implemented** | P1-4, P1-5, P2-1..P2-3 |
| `consume_push_grant` caller and transport | **Deferred, as declared** | Nothing in `app/` calls `consume_push_grant` (C3). Path A's push is **not gated by grants yet**. `observed_base_sha` at consume time is caller-supplied, so the future transport must re-read it remotely (spec step 4). |

## 2. Findings

### P1-1 — Account erasure fails permanently once any activation row exists (existing-contract regression)

- **Cause:** `deny_activation_preflight_mutation` and `restrict_push_grant_mutation` raise on **every** DELETE. They have no GUC escape hatch and no `erase_own_*` SECURITY DEFINER path.
- **How it is reached:** receipts and grants `ON DELETE CASCADE` from `users`, goals, tasks, jobs, worktrees and envelopes. So the existing `erase_account_data()` → `erase_own_execution_authorization_children()` / `db.delete(user)` hits the trigger.
- **E1 (receipt + grant):** `RaiseException: push grants are auditable and cannot be deleted`.
- **E1b (one NO-GO receipt only):** `RaiseException: activation preflight receipts are immutable`. This is exactly what the Founder route writes today with unconfigured settings.
- **Control:** the same erasure without activation rows passes.
- **Precedent:** the repository documented this same class of bug for migrations 0032/0033 in `app/account/erasure.py:752-763`, quoted verbatim: "DELETE /api/account FAILS OUTRIGHT for any owner who has ever run a MainAI goal".
- **Fix:** follow the established pattern.
  - Add an owner-scoped `erase_own_mainai_activation_children()` SECURITY DEFINER function. It sets an erasure GUC that the two DELETE triggers honour.
  - Call it in `erase_account_data()` before the envelope and execution cleanup.
  - Add both tables to the boot privilege policy (see P2-4).
  - Add an erasure regression test that creates a receipt and a grant.

### P1-2 — Exact-SHA CI is red because of this candidate

- **Result:** CI at `37bca08` is `Backend — unit/integration tests` **failure** (2 runs). The log shows **1 failed, 2970 passed**, and the aggregate `All required checks passed` fails.
- **Failing test:** `tests/backend/mainai/test_recovery_takeover_authority_fencing.py::test_downgrading_past_this_migration_fails_loudly_once_a_real_row_exists_not_silently`, with `psycopg2.errors.UndefinedColumn: column execution_authorization_envelopes.base_branch does not exist`.
- **Cause:** the test migrates an isolated database to 0061 and runs the current `execute_takeover()`, which now maps the new envelope columns.
- **Attribution:** parent `c56e075` passes this job (`success` ×2), so the failure is introduced by #280.
- **Undisclosed:** the PR text reports only a selected 132-test suite.
- **Inherited and separate:** `Frontend — npm audit` also fails. That is the inherited FC-4 lane.

### P1-3 — Preflight GO is not evidence: attestations are unverified strings, and the activation SHA is a config value

- **Attestation digests:** `create_preflight_receipt` checks the three digests only against `^[0-9a-f]{64}$`. The router reads them from environment settings.
  - Any 64-hex string yields **GO** (probe P1: `"e"*64` ×3 gives `verdict=go`).
  - Spec §"Founder/admin-attested facts" says: "verified against a pinned trust key/identity", with "attestation issuer, observed time, expiry".
  - None of these are verified, and attestation expiry is not checked.
- **Activation SHA:** `activation_sha = settings.mainai_activation_sha`, an operator-typed string. It is not derived from the running build (P3). The receipt therefore does not "bind code SHA" as spec checklist item P1 requires.
- **Disclosure:** the PR does not declare attestation verification as deferred. A GO verdict that rests on unverifiable inputs is an unimplemented boundary being silently accepted.
- **Fix:**
  - Either verify signed attestation payloads against a pinned key: issuer, expiry ≥ receipt expiry, and a payload digest bound into the receipt.
  - Or make preflight structurally unable to return GO until that verifier exists, with an explicit `attestation_verifier_unavailable` unknown.
  - Derive the activation SHA from the build (for example an image label or `GIT_SHA` baked at build time), not from a free-form setting.

### P1-4 — Grant branch binding is inconsistent with Path A and with #281

- **The rule:** `create_push_grant` requires `worktree.branch == branch` **and** `receipt.integration_branch == branch`.
- **Path A in practice:** task worktrees are created on `claude/mainai-task-*`, so G1 is refused with "preflight receipt bindings differ".
- **The candidate's own fixture** works around this by putting the task worktree **on** `mainai/integration-founder-supervised-v1`.
- **Two readings, both blocking:**
  - (a) The grant means "push the task worktree directly onto the integration branch". That bypasses the PR and CI step, contrary to spec §Push grant state machine ("the remote task branch is absent or is an ancestor…") and §CI.
  - (b) It is a binding bug, and no real task can ever be granted.
- **Composition with #281:** #281's migration `0089_activation_branch_protect` adds `CHECK (branch NOT IN (…,'mainai/integration-founder-supervised-v1') AND branch NOT LIKE 'mainai/%')` on `mainai_task_worktrees`. Composed, grants become impossible by construction.
- **Fix:** bind the grant to the task branch (`grant.branch == worktree.branch`), and bind the receipt's integration branch to the PR **base**, not to the push target.

### P1-5 — Verification PASS is not bound to the job or the commit

- **The check:** grant creation accepts any `verification_passed` event for the task.
- **G2:** the only PASS recorded is for commit `d…d`, yet a grant is minted for `b…b`.
- **Spec requirement:** "verification PASS is durably checkpointed for that job and commit."
- **Consequence:** a later, unverified commit on a retried task inherits an earlier PASS. Mitigation today is only that the Founder reviews the request.
- **Fix:** require a PASS event whose `detail` binds `job_id` and `sha == local_commit_sha`. Prefer a structured column over the JSON detail.

### P2 findings

| ID | Finding | Probe |
|---|---|---|
| P2-1 | The grant does not re-read worktree HEAD: the route passes no `local_head_resolver`, so `current_commit` comes from the DB. Consume also uses the DB column. A grant is minted for a worktree whose path does not exist. The spec requires HEAD equality, and the transport must re-read it. | G3 |
| P2-2 | Spend authorization is not re-checked at grant time or consume time. A revoked spend still yields a grant (G4). A GO receipt can outlive its spend authorization (P2): it lasts 14 minutes against 2 minutes of spend, while the spec says "never beyond … spend grant". | G4, P2 |
| P2-3 | The runtime config digest is not re-checked at grant or consume. The spec says settings changing after receipt creation must invalidate it. | code read |
| P2-4 | The new authority tables are missing from the boot privilege policy and verifier. `mainai_app` (the worker role) keeps default `SELECT/INSERT/UPDATE/DELETE` and can **INSERT an active grant or a GO receipt directly**, with unverified commit and arbitrary facts. Every comparable authority table (`execution_authorization_envelopes`, `provider_spend_authorizations`) is in `apply_mainai_execution_privileges`, with an exact privilege verifier and an `erase_own_*` function. Founder-only minting is therefore an app-layer property only. This is consistent with the envelope posture, so it is P2 and not P1. | F4, F5 |
| P2-5 | A second concurrent grant for the same task (a different idempotency key) is correctly refused by the unique partial index. But the router catches only `ActivationAuthorityError`, so the result is an unhandled `IntegrityError` (HTTP 500), not a typed 409. Exactly one active grant remains. | G6 |
| P2-6 | `consume_push_grant` sets `status='expired'` and then raises, and callers roll back. The row therefore stays `active` forever and blocks every new grant for that task through the one-active-per-task index until a manual cancel. | C2 |
| P2-7 | `canonical_required_checks` accepts only `{name, app_slug}`. Any workflow in the repository (including one added by the task's own PR) can emit a same-named `github-actions` check. #281 binds `app_id`, `workflow_path` and `events` per check. | E2 |
| P2-8 | The idempotency hit returns the existing row before any lock or liveness re-validation. That is safe today because status is preserved and consume re-validates. Document that the returned row is not authority. | G7 (HOLD) |

## 3. Integration blockers (composition)

| With | Result |
|---|---|
| **#276** `09b87e9` | Textually clean (`git merge-tree`). Semantically, all 13 tests in #280's `authority_graph` fixture **error** on the composition. They plan a `repo_edit` with no `target_files`, which #276 correctly refuses (`PlanValidationError: repo_edit requires structured target_files`). With a #276-compatible fixture, my probe suite gives identical results: 12 HOLD / 14 GAP (`results_composition_276.txt`). Of the composed Path A, envelope, CI-wait and demo suites, 161 passed; the only 13 errors are those fixture errors. |
| **#281** `abc88b4` | (1) **Alembic:** two heads off `0088_erasure_completion_phase` (`0089_mainai_activation_security` and `0089_activation_branch_protect`), plus a content conflict in `backend/tests/backend/test_migration_roundtrip.py`. (2) **Contract:** #281's protected-branch CHECK forbids the worktree branch that #280 requires (P1-4). (3) **Check shape:** #281's contract `{name, app_slug, app_id, workflow_path, events}` is rejected by #280's envelope validator ("required checks must contain only name and app_slug", E2). |
| **Codex claim-action** `ecf2f29` (`0089_claim_action_integrity_kernel` → `0090_claim_action_permission_authority`) | This is a third 0089 child of 0088, with the same `test_migration_roundtrip.py` conflict. One of the three must be renumbered on top of the chosen merge order. |
| **Spec transport items** (not candidate defects; future work that must exist before activation) | Consume-time remote base re-read; remote task-branch ancestry check; local HEAD and marker re-read; the push itself and remote-SHA evidence; crash-recovery inspection; TA-12/TA-13 (single commit, regular blobs) at grant **and** consume. |

## 4. Tests run

| Run | Result |
|---|---|
| The candidate's suite, Path A, envelopes, provider-spend, CI-wait, account erasure, migration roundtrip and config contract at `37bca08` | **215 passed** |
| Examiner probes `test_activation_280_exam.py` at `37bca08` | **12 HOLD, 14 GAP** (`results_37bca08.txt`) |
| Same probes on #280+#276 (local merge `861f9eb`) | identical |
| Migration upgrade/downgrade (the candidate's roundtrip test) | pass. The downgrade drops both tables and columns cleanly. |
| Exact-SHA CI `37bca08` | **Backend unit/integration FAIL (P1-2)**, npm audit FAIL (inherited). Alembic, RLS, account lifecycle, E2E ×3, frontend TS, ESLint and builds all `success`. |

## 5. Smallest correction batch

1. Erasure path (P1-1), with a regression test.
2. Make the failing downgrade test independent of the new ORM columns, and fix CI (P1-2).
3. Attestation verifier, or a structural NO-GO without one, plus a build-derived activation SHA (P1-3).
4. Bind the grant branch to the task branch and the receipt integration branch to the PR base (P1-4).
5. Commit-bound and job-bound verification PASS (P1-5).
6. Re-check spend and config at grant and consume, and clamp receipt expiry to spend and envelope expiry (P2-2/3).
7. Add the tables to `apply_mainai_execution_privileges` with an exact verifier, and revoke direct INSERT where an INSERT-only SECURITY DEFINER minting function is feasible (P2-4).
8. Return a typed 409 on unique violations (P2-5).
9. Make the expiry transition durable, for example in its own transaction (P2-6).
10. Adopt #281's check-contract shape, and rebase onto #276 with `target_files` fixtures, renumbering the migration per the chosen merge order.

## Reproduce

```
git worktree add --detach ../wt280 37bca08a287f00b6282222047340bffa949ff788
REDIS_URL=redis://127.0.0.1:<port>/0 probes/activation_280/run.sh ../wt280 -rA
# composition: git worktree add --detach ../wtC280 09b87e9b70dc02889d39c2bae3d56458509d49cb && git -C ../wtC280 merge 37bca08
REDIS_URL=... probes/activation_280/run.sh ../wtC280 -rA
```

Needs Postgres on :5433 and a venv with the backend requirements. The probes run as the restricted `mainai_app` role through the target's conftest.
