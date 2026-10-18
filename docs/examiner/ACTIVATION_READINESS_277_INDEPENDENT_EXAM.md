# Independent examination of activation-readiness PR #277

**Examiner:** Cursor. Claude built the candidate and cannot certify it.
**Verdict: RETURN_TO_BUILDER**
**Candidate:** `claude/mainai-activation-readiness-batch` @ `f0c1d70e0fae44951d82fb24a2fc4bc3188093dc`
**Composition parent:** `claude/activation-base-276-263` @ `93696e22e9d75728a3f9c34a1cd971e14d37f195`
**This evidence branch:** `cursor/examiner-activation-readiness-277-91c1` (exam PR #278)

Do not amend #277, #276, #263, or Codex activation-security. This branch is evidence only.

The activation *implementation* has no P0. It is not activation-ready at this SHA: the composition parent is not frozen #276, product backend CI is red for that reason, and the documented dual-suite CI-wait guarantee is not implemented (Codex PR-identity remainder).

---

## Frozen SHAs verified

| Object | SHA | Result |
|---|---|---|
| #277 head (remote, exact) | `f0c1d70e0fae44951d82fb24a2fc4bc3188093dc` | Matches claimed freeze. Single parent: composition `93696e2`. |
| Composition parent | `93696e22e9d75728a3f9c34a1cd971e14d37f195` | Merge of `6c75174` + `e33f494`. Union-resolved only `github_client.py`, `test_github_client.py`, `docs/BRANCH_REGISTRY.md`. |
| Frozen #276 tip | `09b87e9b70dc02889d39c2bae3d56458509d49cb` | **Not** an ancestor of `93696e2`. |
| #276 TA-8b/TA-10 implementation | `29d230acd1facc469db3d34844c54120c907b71c` | Is an ancestor. |
| #276 demo_1 fixture (`dc67524`) | `dc675247744169d6fa4650f9be4cf1d972aa3792` | **Not** an ancestor. |
| Frozen #263 | `e33f494c4fc53704c494cecea788f3e22be36330` | Is an ancestor. Paginated `list_check_runs` present (`per_page=100`, `_MAX_PAGES=50`). |

**Union conflicts:** both additive. Merge `93696e2` contains Path A’s draft-PR tests (`test_create_pull_request_can_open_a_draft`) **and** #263’s pagination/discovery tests (`test_list_check_runs_follows_pagination`). `github_client.py` has Path A’s 8-line create-PR/commit path **and** #263’s 115-line read/pagination path. No Path A security file (`envelope_scope.py`, `planner.py`, `execution_job.py`) changed in `93696e2..f0c1d70`. TA-10 `git_changed_paths_no_renames` remains.

---

## Scorecard

### 1. Composition integrity — **P1**

Parent is Path A **#276 @ `6c75174`**, not frozen **#276 @ `09b87e9`**. Implementation `29d230a` is included; the demo_1 `target_files` fixture `dc67524` is not.

### 2. CI workflow coverage — **HOLDS**

`push` and `pull_request` both include `mainai/**` plus existing `main`, `claude/**`, `cursor/**`, `codex/**`. Top-level `permissions: contents: read`. No job widens permissions. Canonical 12 required checks bind `github-actions` / app id `15368` / workflow `CI`. Branch-gated VPS/combined/deploy jobs are not required. Live GitHub on `f0c1d70` emitted exactly those 12 names (plus skipped gated jobs and Vercel extras). Artifact upload succeeded on this SHA, so the theoretical `contents: read` vs `upload-artifact` conflict is not a live failure here.

P2: no `persist-credentials: false`; actions pinned by tag not SHA (Claude already deferred).

### 3. Exact-set CI wait — **HOLDS** with one P1 gap and Codex remainder

| Scenario | Verdict |
|---|---|
| >30 check runs / pagination | HOLDS (`_get_paginated_key`, live SHA has 37 runs, test uses 30+7 pages) |
| Missing required check | HOLDS (pending, never PASS) |
| Late-arriving failure *while pending* | HOLDS |
| Skipped / neutral on required | HOLDS (`allowed_conclusions=("success",)` only) |
| Wrong GitHub App / wrong app id | HOLDS |
| Duplicate names / reruns | HOLDS (latest per `check_suite.id`) |
| Mismatched SHA | HOLDS (stale runs ignored) |
| Tampered required-check digest | HOLDS (`RequiredCheckContractError` → failed) |
| Unexpected Vercel checks | HOLDS (evidence only) |
| PR head/base re-read during wait | **PENDING-CODEX** (`poll_ci_wait` has no `get_pull_request`) |
| One GitHub Actions suite green while the other has not created runs | **P1 GAP** (docstring says both suites required; implementation passes on the suites that currently exist) |

Live product CI on `f0c1d70`: two `github-actions` suites, both failed (backend tests + npm-audit + aggregate). Exact-set wait would correctly fail this SHA. npm-audit is FC-4 (separate lane); Codex spec requires keeping it in the required set so the wait stays blocked — not a #277 defect.

### 4. Activation branch/base security — **HOLDS**

Missing config fails closed (`ActivationBaseConfigurationError`). Approved SHA is 40 lowercase hex; worktrees fetch that SHA. Moved integration tip is checked in `create_task_worktree()` before `create_task_worktree_sync()` (no filesystem). Legacy default `claude/det-kommer-mer-879lcm` is refused in activation mode. `mainai/` prefix + named integration branch are protected in Python and in migration 0089 CHECK. Production Path A caller is the async wrapper only. Supervisor `production_worktree.py` still uses local HEAD and ignores activation mode — first-cycle must keep Track B/Supervisor disabled (existing envelope routing, not a new #277 grant).

### 5. Migration 0089 — **HOLDS on this lane; P1 composition collision**

- `revision = "0089_activation_branch_protect"`, `down_revision = "0088_erasure_completion_phase"`, unique head on this tree.
- Upgrade widens `ck_mainai_task_worktrees_branch_not_protected` with `NOT LIKE 'mainai/%'`. Downgrade restores the 0033 set (weakens protection; fail-closed if violating rows exist).
- **Collision:** four in-flight 0089 children of 0088 (this lane, orchestration truth ledger, continuous conversation, Codex claim-action integrity). Codex `mainai-activation-security-spec` still ends at 0088 and *requires* a migration (`approved_base_sha`, `required_check_contexts`). Same `revision` id is **not** duplicated; same `down_revision` **is**. Second-to-land must merge-revision or reparent. Claude documented this. Do not modify either lane.

### 6. Tests and CI

Local (Postgres 16, `lifeos_exam277_py`, no paid providers):

- New suites + config + github_client union + migration roundtrip: **127 passed**
- Four new files collect to **76**; plus pre-existing `test_config_contract.py` (12) = Claude’s **88**
- Independent examiner probes: **17 passed** (`test_activation_readiness_277_independent.py`)
- Path A TA-1..TA-11 + existing ci_wait: **37 passed**, **1 failed** (`test_demo_1`)

GitHub product CI on exact SHA `f0c1d70` (push `39018090339`, PR `39018119396`):

- Backend unit/integration: **1 failed, 3074 passed, 1 skipped** — only `test_demo_1` (`PlanValidationError` missing `target_files`)
- Frontend npm-audit: failed (FC-4, inherited)
- All other required jobs: success
- Aggregate: failure

**demo_1 causation:** #276 @ `6c75174` backend CI is **failure**. Frozen #276 @ `09b87e9` / `dc67524` backend CI is **success** (fixture declares `target_files: ["backend/tests/test_demo_doc_fix.py"]`). #277 did not introduce the failure and did not pick up the fix. Not a Path A security regression.

### 7. First-cycle compatibility — **HOLDS**

`backend/tests/backend/test_first_cycle_probe.py` does not exist (new focused test under TA-8b). Validators accept that single path; envelope of one file admits nothing else. No Track B execution required. No new merge/deploy method. `github_auto_merge_enabled` remains false. `create_pull_request` still exists (Path A drafts); there is no `merge` API.

---

## Findings

### P0

None on the production Path A activation path.

### P1

1. **Composition is not frozen #276.** Parent `93696e2` merges `6c75174`, not `09b87e9`. Product backend CI is red only because of inherited demo_1. Recompose onto frozen #276 + #263; do not rewrite the activation commit.
2. **Dual-suite CI wait overclaim.** `evaluate_required_check_runs` docstring: “push and pull_request suites are each required”. Code passes when every required *name* has ≥1 completed success, even if a second GitHub Actions suite has not created runs. Independent probe `test_indep_gap_one_suite_alone_is_enough_to_pass` characterizes this. Combined with no PR-identity re-read (Codex spec item 12), a wait can satisfy on push-green before PR CI exists. After `satisfied`, polling is a no-op.

### P2

1. `persist-credentials` default true; Actions not SHA-pinned (Claude deferred).
2. Matching ignores stored `workflow` field; same-name github-actions run from another workflow would count (`test_indep_matching_ignores_workflow_field`).
3. String `app_id` `"15368"` does not match int `15368` (fail-closed, acceptable; GitHub JSON uses int).
4. SHA match is case-sensitive (GitHub SHAs are lowercase).
5. Legacy `evaluate_check_runs()` still treats skipped/neutral as pass (not used by `poll_ci_wait`).
6. Activation mode allows any `mainai/*` base, not only `mainai/integration-founder-supervised-v1`.
7. 0089 downgrade narrows branch protection; four sibling 0089 forks from 0088.
8. Supervisor local worktrees ignore activation SHA (Track B must stay disabled).
9. Activation config is lazy (first worktree/PR call), not process-start fail-closed.

### Not defects (keep separate)

- **FC-4 npm-audit** red on this SHA. Required set correctly includes it. Wait stays blocked until FC-4. Do not mix remediation into this lane.
- **Codex PR identity / envelope `approved_base_sha` / attested check set.** Specified, not implemented here. Claude deferred correctly.
- **Path A TA-8b/TA-10.** Present and green in local TA suite.

---

## Required builder action

1. Do not amend frozen `f0c1d70`.
2. Recreate the composition parent as **frozen #276 `09b87e9` + #263 `e33f494`** (union-resolve the same two files).
3. Replay the single activation commit (or equivalent isolated patch) onto that parent.
4. Leave dual-suite / PR-identity to Codex. Do not expand this lane into Track B, FC-4, merge, deploy, or Recall.

Until that recomposition, **#277 is not activation-ready**.
