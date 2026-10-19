# Independent examination of activation-readiness PR #281

**Examiner:** Cursor. Claude built the candidate and cannot certify it.
**Verdict: PASS/FREEZE**
**Candidate:** `claude/mainai-activation-readiness-v2` @ `abc88b4f479489c8c85111d754756058ab77eb22`
**PR:** [#281](https://github.com/d1n095/LifeAI/pull/281) (draft, frozen)
**Corrected composition parent:** `claude/activation-base-276f-263` @ `6871790c8f819d548ffb2ee01f8c24268c09d840`
**This evidence branch:** `cursor/examiner-activation-readiness-281-91c1`

Do not amend #281, #276, #263, #277, frozen #282, Codex activation-security, Track B, Recall, or GitHub settings. This branch is evidence only.

The activation implementation is activation-ready at this SHA for the repository/GitHub-side contract Claude was asked to rebuild. Inherited FC-4 npm-audit remains red on its own lane; exact-set wait correctly stays blocked until that lane composes. Codex PR-identity re-read during wait remains deferred.

---

## Frozen SHAs verified

| Object | SHA | Result |
|---|---|---|
| #281 head (remote, exact) | `abc88b4f479489c8c85111d754756058ab77eb22` | Matches claimed freeze. Single parent: `f9a49c5363eff311778d03c721eeeadbb807aa48`. |
| Activation replay commit | `f9a49c5363eff311778d03c721eeeadbb807aa48` | Cherry-pick/replay of #277 onto the corrected parent. |
| Corrected composition parent | `6871790c8f819d548ffb2ee01f8c24268c09d840` | Merge of frozen #276 `09b87e9` + #263 `e33f494`. |
| Frozen #276 tip | `09b87e9b70dc02889d39c2bae3d56458509d49cb` | **Is an ancestor** (`git merge-base --is-ancestor` exit 0). |
| #276 Demo 1 fixture | `dc675247744169d6fa4650f9be4cf1d972aa3792` | **Is an ancestor**. |
| #276 TA-8b/TA-10 implementation | `29d230acd1facc469db3d34844c54120c907b71c` | **Is an ancestor**. |
| Frozen #263 | `e33f494c4fc53704c494cecea788f3e22be36330` | **Is an ancestor**. Paginated `list_check_runs` and new `list_workflow_runs_for_sha`. |
| Superseded #277 | `f0c1d70e0fae44951d82fb24a2fc4bc3188093dc` | Not this candidate. Cursor exam `f3abe631e23ff77ed059ae712a3f76afc87251b8` returned RETURN_TO_BUILDER. |

Ancestry line (abbrev): `abc88b4` ← `f9a49c5` ← `6871790` (merge `09b87e9` + `e33f494`).

---

## Scorecard

### 1. Composition integrity — **HOLDS**

Parent is frozen **#276 @ `09b87e9`** plus frozen **#263 @ `e33f494`**, not the superseded #276 @ `6c75174` that failed #277. Demo 1 fixture `dc67524` (`"target_files": ["backend/tests/test_demo_doc_fix.py"]`) is present. Independent probe `test_indep_demo_1_fixture_declares_target_files` HOLDS.

### 2. Demo 1 fix — **HOLDS**

Local Path A first-cycle + task-authority + existing ci_wait + `test_demo_1_full_success_path...`: **46 passed**. GitHub `Backend — unit/integration tests` is **success** on both the push suite (`39180026275`) and the pull_request suite (`39180062479`) at exact SHA `abc88b4`. The #277 product-backend red caused by missing `target_files` is gone.

### 3. CI workflow coverage — **HOLDS**

`push` and `pull_request` both include `mainai/**` plus existing `main`, `claude/**`, `cursor/**`, `codex/**`. Top-level `permissions: contents: read`. No `pull_request_target`. Canonical 12 required checks bind `github-actions` / app id `15368` / workflow `.github/workflows/ci.yml` / `allowed_conclusions=("success",)` only. `Frontend — npm audit` remains in the required set. Branch-gated VPS/combined/deploy jobs are not required.

Live GitHub on `abc88b4` emitted exactly those 12 names (plus skipped gated jobs). Product backend jobs succeeded. Frontend npm-audit failed (FC-4). Aggregate `All required checks passed` failed because npm-audit is required — correct fail-closed behaviour, not a #281 defect.

P2 (deferred, same as #277): no `persist-credentials: false`; actions pinned by tag not SHA.

### 4. Event/suite identity binding — **HOLDS** (former #277 P1 closed)

`expected_events(stage=pr_open, …)` into the activation base requires `("push", "pull_request")`. `branch_pushed` requires `("push",)` only. Unsatisfiable head/base (workflow PR filter would never emit a suite) raises `RequiredCheckContractError` and fails the `open_pr` task closed.

`evaluate_required_check_runs` requires every required name to be green in **every** required event suite. Independent probe `test_indep_277_p1_one_suite_alone_no_longer_passes`: push-green with no PR suite is **pending**, never PASS. `test_pr_suite_entirely_missing_never_passes` in Claude's own suite matches.

Checks bind through `check_suite.id` → Actions workflow run `event` + `path`. Same-name run from another workflow (`workflow_path` reject) and unbound suite (`unbound_suite` reject) never count. Independent probes HOLDS.

### 5. Exact-SHA and workflow identity — **HOLDS**

SHA match is case-insensitive (`a`*40 vs `A`*40). Stale SHA ignored. Wrong GitHub App / wrong app id never substitutes. Duplicate names / reruns: latest per `check_suite.id`. Whole-contract digest covers checks + stage + branches + events. Tampered / legacy empty contracts fail closed.

### 6. Pagination — **HOLDS**

`GitHubClient.list_check_runs` and `list_workflow_runs_for_sha` use `_get_paginated_key` (`per_page=100`, `_MAX_PAGES=50`). Independent probe `test_indep_github_client_has_no_merge_method_and_paginates_workflow_runs` HOLDS. No `merge` / `merge_pull_request` method.

### 7. Required-check integrity — **HOLDS**

12-check success-only contract. Vercel extras are evidence only and do not fail a green required set. Skipped required check fails. Second-suite failure fails. npm-audit stays in the required set so the wait cannot PASS until FC-4 composes.

### 8. Fail-closed base configuration — **HOLDS**

Missing activation config fails closed (`ActivationBaseConfigurationError`). Approved SHA is 40 lowercase hex. Activation base must be **exactly** `mainai/integration-founder-supervised-v1` (not any `mainai/*`). Other `mainai/` names are protected branches and are refused as the activation base. Moved integration tip is checked in `create_task_worktree()` before filesystem work. Legacy default `claude/det-kommer-mer-879lcm` is refused in activation mode.

### 9. Integration-branch protection — **HOLDS**

Python `is_protected_branch()` covers the named integration branch and the `mainai/` prefix. Migration `0089_activation_branch_protect` widens `ck_mainai_task_worktrees_branch_not_protected` with `NOT LIKE 'mainai/%'`. Production Path A caller is the async wrapper only. Supervisor local worktrees still ignore activation SHA — first-cycle must keep Track B/Supervisor disabled (existing envelope routing, not a new #281 grant).

### 10. Migration 0089 — **HOLDS on this lane; composition rule unchanged**

- `revision = "0089_activation_branch_protect"`, `down_revision = "0088_erasure_completion_phase"`.
- Unique Alembic head **on this tree**.
- Downgrade restores the 0033 set (weakens protection; fail-closed if violating rows exist).
- Sibling 0089 children of 0088 remain in other lanes (orchestration truth ledger, continuous conversation, Codex claim-action integrity / planned activation-security). **Composition rule:** the later-landing lane re-points `down_revision`. Not a defect of `abc88b4`.

### 11. Path A regressions — **HOLDS**

TA-10 helpers `git_changed_paths_no_renames` / `path_exists_in_git_revision` present (`--no-renames`). `test_first_cycle_probe.py` still absent; validators accept that single path. Local Path A suite **46 passed**. GitHub backend unit/integration **success** on both required suites.

### 12. First-cycle compatibility / no merge — **HOLDS**

No merge/deploy method. `github_auto_merge_enabled` remains false. `create_pull_request` still exists (Path A drafts). `poll_ci_wait` still has no `get_pull_request` / `baseRefOid` re-read (**PENDING-CODEX**, same remainder as #277). Independent review receipts / STOP-before-merge are **out of this PR's scope** and are a separate Cursor implementation batch.

---

## Findings

### P0

None on the production Path A activation path.

### P1

None remaining from the #277 exam. Dual-suite overclaim is closed. Composition is frozen #276 + #263.

### P2

1. `persist-credentials` default true; Actions not SHA-pinned (Claude deferred).
2. PR head/base identity re-read during wait is still absent (`poll_ci_wait` has no `get_pull_request`). Codex remainder.
3. Legacy `evaluate_check_runs()` still treats skipped/neutral as pass (not used by `poll_ci_wait`).
4. Supervisor local worktrees ignore activation SHA (Track B must stay disabled).
5. Activation config is lazy (first worktree/PR call), not process-start fail-closed.
6. 0089 downgrade narrows branch protection; sibling 0089 forks from 0088 remain a composition rule.

### Not defects (keep separate)

- **FC-4 npm-audit** red on this SHA (push `39180026275`, PR `39180062479`). Required set correctly includes it. Wait stays blocked until FC-4. Frozen Cursor #282 (`1e3ee3fd604fc540600f393e215a712b283686d4`) is the remediation lane; do not mix it into #281.
- **Codex PR identity / envelope `approved_base_sha` / attested check set / Git transport / grants / worktree credentials.** Specified elsewhere. Claude deferred correctly.
- **Independent review receipts / STOP-before-merge / no-paid-replan on external CI.** Out of #281 scope; separate Cursor implementation.

---

## Tests and CI

Local (Postgres 16, `lifeos_exam281`, no paid providers, no GitHub mutations):

- Independent examiner probes: **22 passed** (`test_activation_readiness_281_independent.py`)
- New suites + config + github_client + migration roundtrip (with independent probes): **163 passed** in 101s
- Path A first-cycle + task-authority + existing ci_wait + Demo 1: **46 passed** in 74s

GitHub product CI on exact SHA `abc88b4`:

| Suite | Run | Backend unit/integration | Frontend npm-audit | Aggregate |
|---|---|---|---|---|
| push | `39180026275` | success | failure (FC-4) | failure |
| pull_request | `39180062479` | success | failure (FC-4) | failure |

All other required jobs succeeded on both suites. Gated VPS/combined/deploy jobs skipped.

---

## Verdict

**PASS/FREEZE** at `abc88b4f479489c8c85111d754756058ab77eb22`.

Do not amend #281. Do not treat inherited npm-audit red as a #281 defect. Do not treat this exam as independent review of a first-cycle *product* PR — it certifies the activation-readiness replacement only. First-cycle review receipts and STOP-before-merge are a separate implementation.
