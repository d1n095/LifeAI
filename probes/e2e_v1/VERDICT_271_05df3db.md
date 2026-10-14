# Independent examination — Path A SCOPE-1 / CAP-1 / FC-2b / ROUTE-4 PR #271

Exact SHA: `05df3db2cee1aad3607543e548d6fd9cd2c36d31` (`cursor/mainai-execution-path-a-scope-auth-91c1`, parent `d5153eb` = #270).
Examiner: Claude, read-only. Detached worktree, verified unchanged. No live AI call, GitHub write or agent launch. The #271 PR body marks the branch frozen for independent examination. I examined it on my own initiative, and against the #269 findings it targets.

## Verdict: PASS for #271's scope (all four targeted findings closed); one new P2 follow-up. The stack is still not first-cycle-ready, because #270's SPEND-8a/b/c remain open (P1, separate lane, unchanged here).

| Finding (from #269 @ 66c2204) | #269 | #271 @ 05df3db | Evidence |
|---|---|---|---|
| SCOPE-1 path matcher boundary | GAP (sibling file pushed, PR opened) | **HOLDS** | `greeting.py_backdoor.py`, `.backend/...`, `…test_greeting_doc.py.d/x.py`, `mainai_execution_evil.py`: all refused. End to end, the edit is refused ("outside the founder-authorized envelope paths"); nothing is pushed and no PR is opened. |
| SCOPE-1b edge matrix (new) | — | **HOLDS** | 23 cases. Exact file matches, `./` prefix, `dir/` and `dir/**` subpaths: allowed. `/abs`, `..x`, `a/../b`, `Greeting.py`, `file/x`, `.hidden` parts, `testsx/`, `tests_evil/` under `tests/**`, a bare `backend/tests` treated as a file, and `**`, `/`, `./`, `*`, `backend/app/*` grants: all refused. The only "unexpected" result is benign: a candidate equal to the directory itself (`backend/tests` under `backend/tests/`) matches its own grant. |
| CAP-1 `repo_edit` ⇒ `open_pr` | GAP (draft PR opened) | **HOLDS** | `OPEN_PR_CAPABILITIES = {open_pr, create_pr}`. An envelope without `open_pr` opens no PR (`open_pr` → `retryable_failed`). |
| FC-2b CTX trusts prose | GAP (prose-only file pushed) | **HOLDS** for planner-created tasks | `declared_targets_only=envelope is not None`: a file named only in the description is refused ("not an explicit planner target_files path"). |
| ROUTE-4 Supervisor in-flight overlap | GAP | **HOLDS** | A live `supervisor_goal_leases` row blocks Path A dispatch (task stays `ready`). |
| ROUTE-5 reverse direction (new, informational) | — | Acceptable | After a `path_a` → Supervisor switch with a Path A job running, the goal becomes Supervisor-eligible. The Supervisor cannot claim the running job's lease, and Path A re-checks the envelope at every step (verify, finalize, push) and fails closed. |

Unchanged and still holding: the ROUTE matrix; E2E-A' (billable `path_a` goal → verified push + DRAFT PR); FC-1; FC-2a; SCOPE-2; CAP-2; the mid-flight supersede; E2E-B/B2; CTX-1/2; TEST-1; SPEND-1..7/7b/7c/9/10/10b.

Builder suites (30 files, including all `test_mainai_execution_*`, path_routing, supervisor, providers, provider_spend, autonomous_gap, memory_work): **398 passed**. CI at 05df3db: all product checks pass; only the inherited npm audit and its aggregate fail.

Still open from the parent #270 (`billed_chat.py` is not touched by #271), with the same results: **SPEND-8a** (real cost 3.45× the grant when the per-request cost hold is below the token holds), **SPEND-8b** (`ceil(chars/3)` is not an upper bound), **SPEND-8c** (Gemini ignores `max_tokens` and drops thought tokens). Also outside this lane: E2E-C (FC-4 CI gate) and E2E-D receipt (P2-1).

## P2 FC-2b-ins — inserted tasks have no structured targets, so a prose marker still grants edits
`insert_plan_tasks()` (`InsertedTaskSpec`) has no `target_files` field and stores the description verbatim. `_explicit_edit_targets()` parses the last `"\nTarget files:"` line of the description. For an inserted repo_edit task, that line is caller- or AI-written prose.

Probe: on a `path_a` goal with envelope `backend/app/` and `backend/tests/`, a `founder_requirement` insertion with description `"…\nTarget files: backend/app/greeting.py backend/app/farewell.py"` → `farewell.py` was written and pushed, with no refusal.
- **Bounded:** the envelope paths still apply, and insertion requires a founder authority kind.
- **But it contradicts** the #271 claim that "task prose does not grant edit authority". Inserted tasks are also reached by `memory_work_linkage` and (Supervisor-side) `autonomous_gap`.

**Fix:** persist targets structurally, either as a task column/JSON or by having `InsertedTaskSpec.target_files` appended through the same code as `create_plan`. Read only that structured value in `_explicit_edit_targets()`, never a marker parsed from free text.

## Reproduce
```
git worktree add --detach ../wt271 05df3db2cee1aad3607543e548d6fd9cd2c36d31
REDIS_URL=redis://127.0.0.1:<port>/0 probes/e2e_v1/run.sh ../wt271 test_path_a_269.py test_path_a_271.py -k "not e2e_d"   # Postgres :5433 + Redis
REDIS_URL=... probes/path_a_v1/run.sh ../wt271 test_path_a_268.py test_path_a_270.py
```
