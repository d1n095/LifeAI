# Independent examination — Cursor Path A PR #264

Exact SHA: `21ff3cc8ddd8a7da8b2c84a3219f65d752d4ea4b` (`cursor/mainai-execution-path-a-activation-91c1`, base `2fbe20a`).
Examiner: Claude, read-only. A detached worktree was used and left unchanged; no live provider, GitHub write or agent was used.

## Verdict: RETURN_TO_BUILDER

## Evidence
- CI at the exact SHA: everything passes except the inherited `Frontend — npm audit`.
- Builder suites run locally: 110 passed (path_a_activation, github_client, executor, recovery_demos, ci_wait).
- Examiner probes (`run.sh <worktree at 21ff3cc>`): 7 probes, 7 GAP.

| Area | Result | Evidence |
|---|---|---|
| Configurable integration base | PASS (default stale) | `execution_worktree_base_branch()` / `execution_pr_base_branch()` are used for fetch, push target, PR base and recovery lookup. Both bases are in `protected_execution_branches()`. The default is still `claude/det-kommer-mer-879lcm`, so the Founder must set `GITHUB_WORKTREE_BASE_BRANCH`. |
| File context to the editing model | **FAIL — P1** | CTX-1: a task naming no path made the model rewrite `backend/app/core.py`, which it never saw; the write was accepted. CTX-2: a 34,890-char file is shown truncated to 16,000 chars while the model is told to return complete text; a faithful edit deleted 1,053 of 2,000 lines and was accepted. Context is limited to paths named in the description, and nothing restricts writes to files that were fully shown. |
| Targeted-test enforcement | **PARTIAL — P2** | TEST-1: `create_plan` accepts a `repo_edit` with an empty `verification_plan`. Enforcement happens only in `verify_task`, after the paid AI edit and local commit (no push; fail-closed). |
| Failure feedback into retry/replan | PASS | `latest_verification_failure_text` feeds the pytest tail into `_handle_repo_edit`, `_propose_repo_edit` and `trigger_replan` → `propose_plan_via_ai`; covered by the builder tests. When `verify_task` raises the new targeted-test error, no `verification_failed` event exists, so there is no feedback. |
| Draft-PR-only | PASS for Path A; pre-existing exception | `_handle_open_pr` passes `draft=True`. `create_pull_request` still defaults to `draft=False`. The pre-existing Founder-only `/api/agents/.../prepare-pr` (`agent_orchestration.prepare_github_pr`) creates a NON-draft PR and commits via the Contents API to any branch whenever `GITHUB_WRITE_ENABLED=true`. No branch protection exists on the repo. |
| Per-goal provider spend | **FAIL — P1** | SPEND-1: with the default `MAINAI_EXECUTION_REQUIRE_PROVIDER_SPEND=false` and no grant, the AI call goes ahead unbilled (1 provider call, 0 spend events). SPEND-2: the reservation is checked against `resolve_chat_chain()[0]` only. A fallback answer from `openrouter/vendor/unlisted-model` under a grant allowing only `openai/gpt-4o-mini` was accepted, and the ledger recorded it as `openai/gpt-4o-mini`, which is false attribution. SPEND-3: an unpriced model settles at `$0.000000`, so the cost ceiling never advances. |
| Spend failure handling | **FAIL — P2** | SPEND-4/5: an answer larger than its reservation makes `settle_provider_spend_usage` raise; this escapes as a raw `InternalError`, not `ExecutionSpendError`. End to end, the task and job are left `running`. |
| Idempotency | PASS | open_pr reuses an existing PR for the same head/base; a non-force push of the same commit is a no-op; spend `source_ref` is unique per call (over-counts on crash, never under-counts). Note: a reused existing PR may be non-draft. |
| GitHub write boundaries | PASS (Path A) | No merge, force or branch-delete methods. The task branch can never equal the worktree or PR base. Writes stay behind `GITHUB_WRITE_ENABLED`. |

## Minimal fixes
1. Code context:
   - Reject (fail closed) any write to an existing file whose full current content was not in the prompt, including truncated files.
   - Let the planner or the task name the target files, and include those.
   - For files over the cap, refuse or use a diff/patch contract instead of whole-file replacement.
2. Spend:
   - Make `MAINAI_EXECUTION_REQUIRE_PROVIDER_SPEND` default to true, or refuse Path A AI calls without a grant.
   - Pin the call to the reserved provider/model (no fallback outside the grant's allowlist), or reserve per attempted provider.
   - Settle with the provider/model that actually answered.
   - Refuse or conservatively price unpriced models.
   - Convert settle failures into `ExecutionSpendError`.
3. Reject a `repo_edit` without `targeted_tests` in `create_plan`, before any spend.
4. Optional: default `create_pull_request(draft=True)`, and gate or retire the non-draft `prepare_github_pr` route before `GITHUB_WRITE_ENABLED` is turned on.

## Reproduce
```
git worktree add --detach ../wtpatha 21ff3cc8ddd8a7da8b2c84a3219f65d752d4ea4b
probes/path_a_v1/run.sh ../wtpatha        # needs local Postgres :5433 and Redis (REDIS_URL)
```
