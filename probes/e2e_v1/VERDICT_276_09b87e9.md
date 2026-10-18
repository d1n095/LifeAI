# Independent examination — Path A task-authority final PR #276

- Frozen head `09b87e9b70dc02889d39c2bae3d56458509d49cb`.
- Implementation `29d230acd1facc469db3d34844c54120c907b71c`; fixture commits `89087cb`, `6c75174`, `dc67524`.
- Parent `e8914c6010c576e4d0e2aa3c24b69d1ad01daac4` (#273).
- Examiner: Claude, read-only. Detached worktree, verified unchanged after every run. No live provider, GitHub write, merge, deploy or Recall.
- Baseline: my #273 verdict `0ec6ab6` (TA-8b P1, TA-10 P2).

## Verdict: PASS / FREEZE — no P0/P1; two P2 publication-boundary hardening items for the activation push-grant step

The two #273 findings are closed:
- **TA-8b:** an existing focused test listed only in `verification_plan` stays verify-only, and a genuinely new focused test stays creatable.
- **TA-10:** the changed-file set now uses `--no-renames` and exposes both sides of a rename, and the end-to-end rename attack is refused at publication.

The full task-authority, spend, routing, scope and capability preservation suites hold. Exact-SHA CI is green apart from the inherited npm audit.

Two new **P2** publication-boundary hardening items need an *intruder*: something other than the code agent writing into the worktree between write and commit. That is the same threat class as TA-9. Neither is reachable by the code agent alone, since it only returns `{path, content}` pairs written with `write_text` and confined by `resolve()`. They belong to the activation-security push-grant step.

## 1. TA-8b (P1 at #273) — CLOSED
| Probe | Result | Evidence |
|---|---|---|
| TA-8b (#273 probe, unchanged) | HOLDS | planner `target_files=[greeting.py]`, `verification_plan=[tests/test_lesson_guard.py]` (existing guard + active lesson) → authority `verify_only_tests` and `undeclared_focused_tests` contain the guard, `target_files` does not; the agent's rewrite is refused ("not an explicit planner target_files path") |
| TA-8b′ negative before remote mutation | HOLDS | same scenario: **no `claude/mainai-task-*` ref exists on the remote**, no PR; refusal at `_assert_repo_edit_writes_allowed` |
| TA-8c new test creatable | HOLDS | (a) undeclared focused test absent at `base_sha` is lifted by `runtime_editable_targets` and pushed; (b) declared new test pushed. Both pushes = `{greeting.py, test_greeting_doc.py}` |
| TA-8 lesson-injected | HOLDS | lesson tests never enter `undeclared_focused_tests`, so they are never lifted |

Mechanism:
- `persistable_target_authority` persists only the declared `target_files`, plus `undeclared_focused_tests`.
- `runtime_editable_targets(…, exists_at_base=path_exists_at_base(worktree, …))` lifts an undeclared focused test only when it is absent at the worktree's `base_sha`.
- An unknown git state fails closed: it counts as existing, so nothing is lifted.

## 2. TA-10 (P2 at #273) — CLOSED; publication-boundary sweep
| Probe | Result | Evidence |
|---|---|---|
| TA-10a real function | HOLDS | `git_changed_paths_no_renames` on rename + delete → `[backend/app/greeting.py, backend/app/security_guard.py, keep.py]` |
| TA-10b end-to-end rename onto an authorized target | HOLDS | intruder renames out-of-scope `security_guard.py` (a valid module, so tests still pass) onto `greeting.py` → "Refusing to publish 'backend/app/security_guard.py'…"; no push, no PR |
| TA-9 intruder file | HOLDS | (#273 probe) refused before push |
| **TA-13 object type** | **GAP — P2** | intruder replaces the authorized `backend/tests/test_greeting_doc.py` with a **symlink** to an out-of-scope passing test → verification passes (pytest follows the link), the changed path is authorized, and the branch is **pushed with tree mode `120000`** and a draft PR opened. The check validates paths, not git object types or modes. |
| **TA-12 history** | **GAP — P2** | intruder commits an out-of-scope `exfil.py`, then removes it, before the real commit → net `base..HEAD` diff is clean (authorized files only), but **the pushed history contains `exfil.py`** (blob published to the remote) |
| Staging | HOLDS (analysis) | `commit_worktree_changes` = `git add -A` (respects `.gitignore`); every tracked change (add/modify/delete/mode) appears in `--no-renames` name-only output; uncommitted state is never pushed |
| Agent-only symlink/escape | HOLDS (analysis) | code-agent writes are path-validated (`validate_target_path`), confined by `resolve()` to the worktree, and the repository contains no symlinks (`git ls-files -s` mode 120000: none) |

**Fixes (P2, smallest):**
- In `assert_changed_files_within_authority`, or at push-grant creation, require exactly one commit on `base_sha..HEAD` (or validate every commit in the range).
- Require each changed path's new object to be a regular file blob (`git diff --raw --no-renames`: mode `100644`/`100755`, not `120000` or `160000`).

## 3. Full task-authority regression (#273 suite re-run + #276 probes)
| Item | Result |
|---|---|
| TA-1 whitespace split | HOLDS (plan rejected) |
| TA-2 inserted prose | HOLDS: prose-only insertion now **refused at insertion** ("repo_edit requires structured target_files"); structured insertion inserted, no prose target written |
| TA-3 gap/capability injection | HOLDS: child targets = parent created-event targets ∩ scope; injected capability → `GapCapabilityError` |
| TA-4 historical text-only | HOLDS (refused before the code-agent call) |
| TA-5 envelope rechecked | HOLDS |
| TA-6 prose cannot publish | HOLDS |
| TA-7 created-event provenance | HOLDS: `{plan_version, target_files, verify_only_tests, undeclared_focused_tests, target_source}`; description marker ignored |
| TA-8 / 8b / 8c | HOLDS (§1) |
| TA-9 / TA-10 | HOLDS (§2) |
| TA-11′ retry / replan | HOLDS: retry keeps identical authority; replan writes fresh created-event authority (`target_files=[greeting.py]`, focused test in `undeclared_focused_tests`, `target_source=planner`). The #273-era expectation that "verification tests are auto-editable" is superseded by design. |
| Recovery | HOLDS (static; unchanged from #273: no prose reconstruction; salvage covered by the actual-diff check) |
| Explicit PR capability (CAP-1) | HOLDS |

## 4. Spend and routing preservation
- **HOLD:**
  - SPEND-1..7, 7b, 7c, 8, 9, 10, 10b, 8a/8b/8c;
  - TEST-1;
  - CTX-1′/CTX-2′ (`probes/path_a_v1/test_path_a_276_ctx.py`): the original CTX-1/CTX-2 probes construct a `repo_edit` with no `target_files`, which is now correctly refused at plan time, so they were re-expressed under the structured contract;
  - SCOPE-1, SCOPE-1b, SCOPE-2; CAP-1, CAP-2; FC-1, FC-2a, FC-2b, FC-2b-ins (now refused at insertion); ROUTE-1/2/3, ROUTE-4, ROUTE-5;
  - E2E-A′ (billable `path_a` goal → verified push + draft PR).
- **Expected contract changes, not regressions:**
  - The legacy E2E-A/B/B2/C probes plan without `target_files` and are now refused fail-closed.
  - E2E-D needs Track B (not in this lineage).

## 5. CI and inherited failures
- **Exact-SHA CI `09b87e9`:** all product checks success (two runs each), **including `Backend — unit/integration tests`**. Only `Frontend — npm audit` and its aggregate `All required checks passed` fail. That is the inherited FC-4 lane: #275 is RETURN_TO_BUILDER for A6/A7, and its successor is not in this lineage.
- At the implementation SHA `29d230a` the backend job was `cancelled` (superseded by later pushes); that is not a failure of this candidate.
- **Demo 1:** at `6c75174`, `test_mainai_execution_demos::test_demo_1…` failed with `PlanValidationError` (its planner JSON had no `target_files`). `dc67524` adds `"target_files": ["backend/tests/test_demo_doc_fix.py"]` to that fixture, the correct declaration under the new contract with no weakening. **Closed:** passes locally and in CI.
- **Local full `tests/backend` at `09b87e9`:** **2979 passed, 11 failed, 1 skipped**.
  - All 11 failures reproduce identically when re-run in isolation on the **parent `e8914c6`** (11 failed / 77 passed on both).
  - They are local-environment causes, absent in CI:
    - `blocking_migrations:unknown` startup-readiness (`test_safe_internal_run` ×3, `test_workforce_activation_prep` ×4);
    - `ruff` not installed (`test_scoped_development_supervisor` ×2);
    - docker entrypoint exit 127 (`test_memory_source_units` ×1);
    - a migration downgrade fencing test (`test_recovery_takeover_authority_fencing` ×1).
  - None is introduced by #276. CI `Backend — unit/integration tests` passes at `09b87e9`.

## 6. Integration compatibility
- **#263:**
  - `09b87e9` + `e33f494` merge with only the two known additive conflicts (union-resolved).
  - The composed `test_github_client.py` + `test_mainai_execution_ci_wait.py` + `test_mainai_execution_path_a_task_authority.py` + `test_mainai_execution_demos.py`: **62 passed**.
  - TA-8b/8c/10a/10b/11′ HOLD on the composition.
- **Activation-security substrate (Codex spec `8a5bdaf`):**
  - Compatible: `path_exists_at_base` uses the worktree's recorded `base_sha`, which the spec binds to the approved SHA.
  - The actual changed-file check must run at **push-grant creation and again at consumption**. Today it runs inside `_finalize_repo_edit` just before the push.
  - The grant's `local_commit_sha` binding should adopt the TA-12/TA-13 fixes: a single commit, regular blobs only.
  - Proposal mode resolves existence at the running checkout's `HEAD`. That is acceptable because proposal mode is not an activation path, but it must never be used for remote effects.
- **My #277** (frozen, built on `6c75174`): #276 final adds only the Demo 1 fixture and registry on top, so composition is unaffected apart from a registry union.

## Reproduce
```
git worktree add --detach ../wt276f 09b87e9b70dc02889d39c2bae3d56458509d49cb
REDIS_URL=redis://127.0.0.1:<port>/0 probes/e2e_v1/run.sh ../wt276f test_task_authority_273.py test_task_authority_276.py -k "ta or rename or symlink or intermediate"
REDIS_URL=... probes/path_a_v1/run.sh ../wt276f test_path_a_268.py test_path_a_270.py test_path_a_276_ctx.py
REDIS_URL=... probes/e2e_v1/run.sh ../wt276f test_path_a_269.py test_path_a_271.py -k "not e2e_d"
# composition: merge e33f494 into a detached 09b87e9 (merge=union for docs/BRANCH_REGISTRY.md and tests/backend/test_github_client.py)
```
