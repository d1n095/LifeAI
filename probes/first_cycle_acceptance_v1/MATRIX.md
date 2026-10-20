# First-cycle final independent examination matrix

**Evidence classes:**

| Class | Meaning |
|---|---|
| **E** | Executable locally, network-free, synthetic credentials, hermetic git. Gated by `run_acceptance.sh` and `summarize.py`. |
| **C** | Exact-SHA GitHub Actions results, read through the unauthenticated public API (read-only, no credentials). Gated. |
| **F** | Founder / GitHub configuration or organisational evidence that **cannot be verified locally**. It must be attested separately and is **never** implied by a green executable gate. |

**Profiles:**
- `cursor`: the corrected Cursor staging successor (the #287 successor).
- `codex`: Codex's composed security candidate (transport, grants and the #280 replacement on top of accepted staging).

**Columns:**
- **Owner:** who must make the row pass.
- **Status @ #287:** the baseline recorded at `1f1819a` (`baseline_run_287.txt`).

## 1. Authorization (Founder authority, envelopes, grants)

| ID | Check | Class | Suite (`out/<profile>-<sha>/…`) | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| AU-1 | Founder-only APIs: role **and** fixed `FOUNDER_USER_ID`; body cannot set authority | E | `authorization_grants` (F1–F3, G5) | all pass | Codex | n/a (#280 not composed); HOLD at #280 |
| AU-2 | Preflight GO requires verified attestations (pinned key, expiry) and a build-derived activation SHA | E + F | `authorization_grants` (P1, P3) + attestation material | pass; attestation keys attested | Codex / Founder | GAP at #280 (P1-3) |
| AU-3 | Grant bound to the task branch, the integration branch bound to the PR base; compatible with the #281 protected-branch CHECK | E | `authorization_grants` (G1) | pass | Codex | GAP at #280 (P1-4) |
| AU-4 | Grant requires a verification PASS bound to job **and** commit; HEAD and spend re-read | E | `authorization_grants` (G2–G4, P2) | pass | Codex | GAP at #280 (P1-5, P2) |
| AU-5 | One-shot, race-safe consumption; immutable terminal states; RLS | E | `authorization_grants` (C1, D1–D3, G7) | pass | Codex | HOLD at #280 |
| AU-6 | Runtime role cannot mint grants or GO receipts directly | E | `authorization_grants` (F4, F5) | pass | Codex | GAP at #280 (P2) |
| AU-7 | Exact-SHA review receipts: runtime cannot write; bound to repo/PR/head/base/instruction/envelope; negative/stale blocks | E | `review_stop` (R1, R4, S1–S4) | pass | Cursor | HOLD |
| AU-8 | Reviewer ≠ builder normalised; receipt ordering not caller-timestamped | E | `review_stop` (R2, R3) | known P2 | Cursor | P2 |

## 2. Transport (credentials, push)

| ID | Check | Class | Suite | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| TR-1 | No token in remote URL or worktree `.git/config`; worker/test/reviewer cannot read writable credentials | E | `candidate_harness` rows CRED-1/2/3 | PASS (codex profile) | Codex | FAIL (strict xfail; Codex-owned) |
| TR-2 | Production push requires an exact-SHA grant (E2E-3); grant interface present (GRANT-PROD) | E | `candidate_harness` E2E-3, GRANT-PROD | PASS (codex profile) | Codex | FAIL / BLOCKED |
| TR-3 | Harness credential isolation: canary never invoked, no secret persisted, no repo-tree matrix | E | `candidate_harness` safety, `credential_canary` C0–C3, C5 | all pass | Cursor | HOLD |
| TR-4 | `extra_env` cannot re-enable a credential helper | E | `credential_canary` C4 | pass | Cursor | P2 |
| TR-5 | Runtime GitHub App identity, permission manifest, no classic PAT, token scope | F | Founder attestation | attested | Founder | unverified |

## 3. Publication content

| ID | Check | Class | Suite | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| PU-1 | Structured target authority, rename/deletion exposure (TA-8b/8c/10a/10b/11′) | E | `task_authority` | HOLD | — | HOLD |
| PU-2 | Single-commit history (TA-12), regular blobs only, no symlink (TA-13) or gitlink (PUB-2) | E | `task_authority` TA-12/TA-13, `candidate_harness` TA-12/TA-13/PUB-2 | HOLD/PASS (codex profile) | Codex | GAP / FAIL (known) |
| PU-3 | Draft PR only; no merge implementation; `merged=false` everywhere | E | `review_stop` (S1, S2) | pass | Cursor | HOLD |

## 4. CI

| ID | Check | Class | Suite | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| CI-1 | 12 required checks + aggregate `success` in push **and** pull_request suites at the exact SHA | C | `ci_exact_sha` | 12/12 × 2 | all | PASS |
| CI-2 | Workflow contract: `ACTIVATION_REQUIRED_CHECKS` = `ci.yml` job names; FC-4 steps present; event binding | E | `backend_regression` (ci_workflow_contract, ci_wait suites) | pass | #281 lane | HOLD |
| CI-3 | FC-4 npm-audit policy (A6/A7/A10/A11) | E | `fc4_policy`, `fc4_bypass` | pass; B1/B2/B3/B7/B8 known P2 | Cursor | HOLD (+P2) |
| CI-4 | Legacy `evaluate_check_runs` (skipped-as-pass) removed | E | `candidate_harness` CI-6 | PASS (final) | #281 lane | FAIL (dead code) |
| CI-5 | Ruleset on `mainai/integration-founder-supervised-v1`: PR required, 12 checks bound to app 15368, no bypass actors, no force push or deletion | F | GitHub settings attestation | attested | Founder | unverified |
| CI-6 | Live `npm audit` against the registry (network) | C | CI job `Frontend — npm audit` | success | — | PASS (in CI-1) |

## 5. Spend

| ID | Check | Class | Suite | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| SP-1 | Reservation bounds each paid request; no unauthorised fallback; settlement correct (SPEND-1..10, 8a/b/c) | E | `spend` | all HOLD | — | HOLD |
| SP-2 | **Zero additional paid calls after any external failure** (19 origins) | E | `stop_classification` FO-1..FO-6 | all pass | Cursor | **FAIL (P1-2b): 5/19** |
| SP-3 | Local verification failures keep retry and replan | E | `stop_classification` LR-1, LR-2 | pass | Cursor | HOLD |
| SP-4 | Model-controlled text cannot select the external class | E | `stop_classification` LR-3, `review_stop` N4 | pass | Cursor | P2 |
| SP-5 | Spend grant amount, providers and models for the first cycle | F | Founder spend authorization | attested | Founder | unverified |

## 6. STOP

| ID | Check | Class | Suite | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| ST-1 | Goal-scoped STOP: siblings parked; dispatch, retry, takeover, readiness and replan refuse; ordinary blocked goals unaffected | E | `review_stop` (S7, S10–S13) | pass | Cursor | HOLD |
| ST-2 | **Running sibling at STOP: cancel-requested; final pre-push check refuses; no paid call** | E | `stop_classification` RS-1..RS-3, `review_stop` S9 | pass | Cursor | **FAIL (P1-1b)** |
| ST-3 | External CI failure, timeout, cancel, negative review: no replan; repeated events idempotent | E | `review_stop` (P1, P2, S4, S6) | pass | Cursor | HOLD |
| ST-4 | Founder can close a stopped task; a receipt recorded after CI is evaluated | E | `review_stop` (S8, S5) | known P2 | Cursor | P2 |

## 7. Account erasure

| ID | Check | Class | Suite | Pass criterion | Owner | Status @ #287 |
|---|---|---|---|---|---|---|
| ER-1 | `DELETE /api/account` succeeds after preflight receipts and push grants exist | E | `authorization_grants` (E1, E1b) | pass | Codex | GAP at #280 (P1-1) |
| ER-2 | Erasure succeeds with an owner-scoped review receipt (0080 trigger) | E | `review_stop` (R5) | pass | Cursor / registry owner | P2 (pre-existing) |
| ER-3 | Migration roundtrip; single non-forked 0089 head | E | `backend_regression` (migration_roundtrip) + head check | pass | Codex | HOLD at #287 (single head); conflict pending with Codex 0089 |

## Founder / GitHub evidence required before GO (class F, never covered by the executable gate)

1. Integration branch `mainai/integration-founder-supervised-v1` exists at the approved base SHA.
2. A ruleset on that branch: PR required, the exact 12 required checks bound to app 15368, no bypass actors, no force push or deletion, Founder-only merge.
3. Runtime GitHub App: identity, repository selection, exact permission manifest, no forbidden permissions; no classic PAT.
4. Signed attestation material and pinned verification key for preflight (token, ruleset and Founder attestations).
5. The first-cycle spend grant (cap, provider and model allow-list) and the envelope profile.
6. Reviewer identity and the receipt-recording runbook (privileged path only).
7. Revocation of the incident token (performed) and the publication method for evidence (pending).

## Gate rule

An independent **PASS/FREEZE** for either profile requires all of the following:
- the executable gate is `PASS`: zero blockers and zero regressions;
- CI-1 is green at the exact SHA;
- every remaining P2 is listed;
- for the final activation decision, every class-F item is attested.

**RETURN_TO_BUILDER** if any required row fails, or if any non-required row regresses against its recorded baseline.


## Additions (FO-6/FO-7 exact paths, grant contract, transport contract, concurrent STOP, erasure)

| ID | Check | Class | Suite | Owner | Status (#290 6c98814 / #291 0b1871b) |
|---|---|---|---|---|---|
| FO-7x | Approved-base fetch at worktree creation fails closed (typed) | E | `stop_classification` H | Cursor | FAIL / **PASS** |
| FO-7n | Recovery/operator fetch failure is typed, never "no branch" | E | `stop_classification` H | Cursor | PASS / PASS |
| FO-6x, RL-1..3, FO-6b | Bare RuntimeError / crash at the push boundary on a governed goal: no paid re-run, terminal, Founder-visible (**P1**) | E | `stop_classification` H, K, F | Cursor | **FAIL / FAIL** (3 jobs, 3 paid agent runs, stranded `ready`) |
| CS-1..3 | Concurrent STOP before draft-PR publication: no PR, no completion, no paid call | E | `stop_classification` J | Cursor | PASS / PASS |
| ER-4 | Account erasure after a complete cycle (push, draft PR, STOP-before-merge) | E | `stop_classification` J | Cursor | PASS / PASS |
| GC-1..GC-8 | One-use, race-safe, expiring, cancellable, exactly bound push grant; distinct draft-PR grant; no re-issue without fresh preflight; typed denial | E | `grant_contract_v1` (adapter) | Codex | BLOCKED (no grant API) / BLOCKED. On #280: GC-1..5 pass, GC-6 BLOCKED, GC-7 GAP, GC-8 BLOCKED. |
| TC-0..TC-3 | Production git never consults global/system helpers, env-injected helpers or askpass | E | `transport_contract_v1` (canary, sandbox, localhost 401) | Codex | TC-1/TC-3 FAIL, TC-2 PASS (both) |

Integration contracts K1..K12 and the adapter strategy: `../grant_contract_v1/INTEGRATION_CONTRACTS.md`.

## Application readiness (added for #292; `combined` profile = Codex integration candidate, every row required)

| ID | Check | Class | Suite | Owner | Status @ #292 5e3cd01 |
|---|---|---|---|---|---|
| AP-1 | Every mounted /api route: member and role-only "founder" impostor get 401/403; anon only reaches public entry points | E | `app_readiness` AR-1 (131 routes) | all | PASS |
| AP-2 | Founder STOP over HTTP: authz, CSRF, validation, no epoch change on refusal, rate limit, fail-closed reads | E | `app_readiness` AR-2..AR-4 | Cursor | PASS |
| AP-3 | Dependency failure / stale heartbeat never reported reachable/alive; account isolation of the snapshot | E | `app_readiness` AR-5, AR-6 | Cursor | PASS |
| AP-4 | Recall unmounted (non-vacuous detector); GO never derived from flags; safe default never pushes/opens PR | E | `app_readiness` AR-7, AR-13 | Cursor | PASS |
| AP-5 | Status reads and safe-mode chat make no provider call / egress | E | `app_readiness` AR-8, AR-12 | Cursor | PASS |
| AP-6 | Concurrent duplicate paid dispatch refused (direct + HTTP) | E | `app_readiness` AR-9, AR-9h | Cursor | **FAIL (P1)** 2 paid calls |
| AP-7 | Concurrent duplicate delegation refused | E | `app_readiness` AR-10 | Cursor | **FAIL (P2)** 20/20 |
| AP-8 | `independently_verified` never shown for builder self-verification or zero-check work | E | `app_readiness` AR-11, AR-11b | Cursor | **FAIL (P1)** |
| AP-9 | Founder-status UI never keeps positive health/STOP claims after a failed poll | E (manual, live stack) | `proxy_v1/live_stack.sh` | Cursor | **FAIL (P2)** |
| PX-1 | Proxy never leaves /api/ (encoded traversal) | E | `proxy` | frontend lane | FAIL (pre-existing; known P2 for cursor/codex, REQUIRED for combined) |
| PX-2/PX-9 | Client X-Forwarded-For not trusted; login per-IP limit not bypassable end to end | E | `proxy` (Dockerfile uvicorn flags) | frontend + backend lane | FAIL (pre-existing; same) |
| PX-3..PX-6 | Host not forwarded; hop-by-hop stripped; Set-Cookie preserved; backend-down structured 502 | E | `proxy` | frontend lane | PASS |
