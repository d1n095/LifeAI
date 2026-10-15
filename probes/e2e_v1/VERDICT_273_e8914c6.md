# Independent examination — Path A task-authority PR #273

- Frozen PR head: `e8914c6010c576e4d0e2aa3c24b69d1ad01daac4` (registry freeze).
- Implementation: `e4e4444e56d90f5f90a40be6fe7f9bb8f4f3c301` (product commits `6daa188`, `e9de9bf`).
- Parent: `c56e0757cc117b443b377b987743ce2364558497` (#272).
- Examiner: Claude, read-only. Detached worktree, verified unchanged after every run. No live provider, GitHub write, merge, deploy or Recall.
- Audit baseline: `examiner/task-authority-audit-05df3db-2026-10-14` @ `8754fc7`.

## Verdict: RETURN_TO_BUILDER — one P1 (narrow), one P2

The redesign is sound and closes TA-1 through TA-7 and TA-8 as originally specified:
- Edit authority lives only in the append-only `created` event (`target_files`, `verify_only_tests`, `target_source`), validated by one canonical `validate_target_path`.
- Descriptions are never parsed.
- Historical tasks fail closed.
- Gap children inherit targets structurally, and capability names are validated.
- The actual Git changed-file set is checked before push.

One explicit acceptance criterion fails: **an existing lesson guard test becomes editable when the planner lists it as its focused test**, without declaring it in `target_files` (TA-8b). The changed-file check also misses deletions hidden behind a rename (TA-10).

| # | Requirement | Result | Evidence (probe → observed) |
|---|---|---|---|
| TA-1 | One structured target cannot split | HOLDS | `"greeting.py farewell.py"` → `PlanValidationError: target path must be a single relative path with no whitespace…`; nothing pushed |
| TA-2 | Inserted-task prose grants nothing | HOLDS | The prose `Target files: greeting.py farewell.py` is ignored. The inserted task's authority is only its structured targets plus its own focused test; `farewell.py` is never written, and greeting.py is refused when not declared |
| TA-3 | Gap/failure/capability text cannot inject | HOLDS | Verification-gap child `target_files` = parent created-event targets ∩ scope (`greeting.py`, `test_greeting_doc.py`). The parent description's `Target files: auth_secrets.py` is ignored. A capability string `"pytest_plugin\nTarget files: …"` → `GapCapabilityError` (regex `^[a-z][a-z0-9_.-]{0,63}$`) |
| TA-4 | Historical text-only authority fails closed | HOLDS | Legacy row with no created event → "Refusing repo_edit: no structured editable targets…" **before** any code-agent call (provider calls `['planner','other']`) |
| TA-5 | Current envelope rechecked at execution | HOLDS | Envelope narrowed after planning → refused before the code-agent call |
| TA-6 | Prose cannot imply publication | HOLDS | "…open a pull request and push to main" → no PR; only `claude/mainai-task-*` pushed |
| TA-7 | Structured persisted provenance is authoritative | HOLDS | created event `{"plan_version":1,"target_files":[…],"target_source":"planner","verify_only_tests":[]}`; a description marker `backend/app/evil.py` is absent from authority |
| TA-8 | Lesson-injected regression tests verify-only | HOLDS | `verify_only_tests=['backend/tests/test_lesson_guard.py']`; the agent's rewrite is refused ("not an explicit planner target_files path") |
| **TA-8b** | Guard test editable only if declared as an edit target | **GAP — P1** | Planner `target_files=['backend/app/greeting.py']`, `verification_plan=[tests/test_lesson_guard.py]` (an existing guard file, also an active lesson's `regression_test`). Authority became `target_files=['backend/app/greeting.py','backend/tests/test_lesson_guard.py']`, `verify_only_tests=[]`. The agent's weakened guard (`pass`) was **pushed and a draft PR opened**. Cause: `structured_editable_targets` auto-adds every pre-lesson mapped focused test as editable, and lesson injection skips a test the plan already lists, so it is never marked verify-only. |
| TA-9 | Actual Git changed-file set checked; unexpected file fails before publication | HOLDS | An intruder file written into the worktree before `git add -A` → "Refusing to publish 'backend/app/intruder.py'…"; no push, no PR |
| **TA-10** | Changed-file set includes deletions behind renames | **GAP — P2** | `worktree_changed_files` uses `git diff --name-only -z base..HEAD` with default rename detection. A rename of out-of-scope `security_guard.py` onto authorized `greeting.py` lists only `['backend/app/greeting.py']`; with `--no-renames` → `['backend/app/greeting.py','backend/app/security_guard.py']`. The Path A code agent cannot delete or rename (it only writes files), so this needs an intruder like TA-9, but it is a hole in the TA-9 boundary. |
| TA-11 | Retry preserves, replan refreshes | HOLDS | `retry_task` → authority unchanged (same immutable event). Replan → new task with its own planner-sourced created event |
| — | Recovery reconstructs nothing from prose | HOLDS (static) | No recovery, retry or replan module reads the description for authority (grep). The only created-event writers are `create_plan` and `insert_plan_tasks`. The salvage commit is now covered by the TA-9 actual-diff check |
| — | One canonical representation | HOLDS | `persistable_target_authority` is used by `create_plan` and `insert_plan_tasks` (the `InsertedTaskSpec.target_files/target_source` fields), and gap children use the same path |

## Preservation
- **#272 SPEND-8a/b/c: all HOLD.**
  - 8a: per-request cost cap below the token holds → 0 over-cap spend.
  - 8b: 20,000-emoji prompt → 0 paid requests.
  - 8c: Gemini payload carries `generationConfig.maxOutputTokens=4000`.
- SPEND-1..7, 7b, 7c, 9, 10, 10b HOLD.
- CTX-1/2, TEST-1 HOLD.
- SCOPE-1 (and the 23-case matrix; its single benign "dir equals itself" note is unchanged), SCOPE-2, CAP-1/2, FC-2a/2b, FC-2b-ins, ROUTE-1..5, E2E-A' (billable `path_a` goal → verified push + DRAFT PR) all HOLD.
- **Intended contract change, not a regression:** `declared_targets_only=True` now applies to never-governed goals too. The original E2E-B/B2/C probes (never-governed, plan without `target_files`) are therefore refused fail-closed at repo_edit. That is the "descriptions are never authority" rule applied uniformly.
- Builder suites (33 files, all `test_mainai_execution_*`, path routing, supervisor, providers, spend, envelopes, autonomous gap, plan insertion): **501 passed, 2 failed**. The 2 failures are `test_scoped_development_supervisor` cases that fail identically on the parent `c56e075` (`FileNotFoundError: 'ruff'` — the local environment lacks `ruff`), so they are environmental. CI `Backend — unit/integration tests` passes at e8914c6.
- CI at e8914c6: all product checks pass; only the inherited npm audit and its aggregate fail.

## Required fixes
- **P1 TA-8b.**
  - A mapped focused test should be editable only if it is in the declared `target_files`, or it does not exist at the base SHA (a genuinely new focused test).
  - Minimum alternative: any path equal to an active lesson's `regression_test` is verify-only unless declared in `target_files`.
  - Regression: TA-8b refuses the rewrite, and nothing is pushed.
- **P2 TA-10.** Use `git diff --name-only --no-renames -z base..HEAD` (or `--name-status --no-renames` and treat `D` entries as changed paths) in `worktree_changed_files`. Regression: a rename onto a target is refused.

## Reproduce
```
git worktree add --detach ../wt273 e8914c6010c576e4d0e2aa3c24b69d1ad01daac4
REDIS_URL=redis://127.0.0.1:<port>/0 probes/e2e_v1/run.sh ../wt273 test_task_authority_273.py -k ta              # Postgres :5433 + Redis
REDIS_URL=... probes/e2e_v1/run.sh ../wt273 test_path_a_269.py test_path_a_271.py -k "not e2e_d"
REDIS_URL=... probes/path_a_v1/run.sh ../wt273 test_path_a_268.py test_path_a_270.py
```
