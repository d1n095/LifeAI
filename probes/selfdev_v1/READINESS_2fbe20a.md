# MainAI self-development readiness — read-only examination

Audited SHA: 2fbe20aacf1203fc0e16d216ef55b666b2181619 (`codex/founder-alpha-final-composed-candidate`).
GitHub default branch `claude/det-kommer-mer-879lcm` is at 64c07d5 (2026-09-01) and does NOT contain this base.
Examined 2026-10-11 from a detached worktree; nothing modified; no autonomous execution activated.

## Evidence run
- 150 existing tests pass at 2fbe20a across the worktree, demos, ci_wait, replan, recovery_salvage, executor,
  github_client, development_operator, supervisor_production_entry, autonomous_gap_worker_live_loop and
  autonomous_task_provider_spend_e2e suites (110 s).
- The tests use real `git` against a local bare remote with GitHub API calls patched; the end-to-end demos stub the LLM.
- GitHub: 0 `claude/mainai-task-*` branches and 0 MainAI-authored PRs, so the loop has never run for real.

## Capability matrix
| # | Capability | Verdict | Code path |
|---|---|---|---|
| 1 | Identify/prioritize needs | PARTIAL | work_candidates (founder authorize -> create_goal), project_entities, mainai_vision.gap_generator (propose only), autonomous_gap (child tasks inside an authorized goal). Nothing scans repo/CI/test state for needs; mainai_executive.lookaround not wired. |
| 2 | Plan + assign | PASS (Path A) | mainai_execution.planner.propose_plan_via_ai -> create_plan (task_type allowlist, DAG cycle check); routers/mainai_execution POST /goals, /goals/{id}/plan; worker dispatch. |
| 3 | Repo access, isolated worktree, code modification | PARTIAL | worktree.create_task_worktree (per-job, ownership marker, fresh base from get_ref); execution_job._handle_repo_edit. BLOCKERS: base hard-coded to stale default branch (worktree.BASE_BRANCH); code agent sees only task.description and must return whole files (no file contents in prompt). |
| 4 | Testing + failure interpretation | PARTIAL | verify.verify_task (targeted_tests only; empty plan passes); _run_pytest in worktree; replan.trigger_replan re-prompts with goal text only (failure output never reaches the model); lessons after exhaustion. |
| 5 | Push, remote-SHA verification, PR | PARTIAL | worktree.push_worktree_branch (non-force, get_ref == local HEAD); _handle_open_pr (idempotent per head/base); ci_wait polls exact head SHA. PRs are non-draft and target the stale default branch. Behind github_write_enabled=False. |
| 6 | Independent exam + Founder approval | PARTIAL | approval.py per-task gates (standard_repo_work = all AUTO); no merge path. mainai_verification_registry and dev_director builder/examiner are not connected to execution-loop PRs. |
| 7 | Persistent state, recovery, dedup | PASS | mainai_jobs lease_generation fencing, checkpoints, recovery_* (salvage reuses worktree progress without a new AI call), idempotent push/PR, supervisor_goal_leases. |
| 8 | Security boundaries | PARTIAL | PASS: GitHub client has no merge/branch-delete/deploy; never force-pushes; path confinement; envelope-scoped supervisor with remote write disabled. GAP (catalog evidence, not exercised): mainai_app holds INSERT on mainai_task_events and INSERT/UPDATE on execution_authorization_envelopes and provider_spend_authorizations with no guard triggers, so authority rows are runtime-writable. |

## Duplicate coordination / conflicting truth
Path A (mainai_execution per-job worktree + mainai_jobs) vs Path B (development_supervisor per-goal worktree + supervisor_goal_leases,
remote write off); agent_coordination leases; workforce / mainai_workforce epochs; dev_director (in-memory); mainai_level2 / canonical_supervisor;
unmerged Grandmaster SQLite store and Orchestration Ledger; manual docs/BRANCH_REGISTRY.md; stale GitHub default branch vs composed candidate.

## Minimum blockers to the first real cycle
1. Base branch: make BASE_BRANCH configurable and point it at the Founder Alpha integration branch.
2. Repository context: give the code agent the current contents of the files it edits (read before write) and accept edits against them.
3. Verification: require a non-empty targeted_tests plan for repo_edit; feed pytest/CI failure output into retry/replan.
4. Draft PR + independent review: open PRs as draft; record an examiner verdict in mainai_verification_registry before ready-for-review.
5. Authority rows: make approval events, envelopes and spend grants Founder-only writes (not runtime-writable) before enabling unattended mode.
6. Configuration: a scoped GitHub token (contents+pull_requests on this repo only) and github_write_enabled=True for one supervised run.

## Shortest safe demonstration
Path A only, Founder-created goal, one tiny low-risk change (e.g. a docstring/test addition) with a targeted test,
blockers 1, 2, 3 and 4 fixed, a draft PR against the integration branch, independent examination, and the Founder merges manually.
