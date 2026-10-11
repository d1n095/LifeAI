# Critical path: MainAI running, improving herself, taking over coordination

Base: 2fbe20aacf1203fc0e16d216ef55b666b2181619. Consolidates READINESS_2fbe20a.md and AGENT_SUPERVISOR_AUDIT_2fbe20a.md.
Examiner document (read-only). No candidate code changed; no agent invoked; nothing merged or deployed.

## Verified facts this plan rests on
- Path A loop exists and passes 150 tests (real git with a local remote; GitHub API patched; LLM stubbed). Never run for real.
- Agent CLIs installed on the Founder machine (verified with `command -v` and `--help` only; NOT executed with a prompt, so auth is UNKNOWN):
  `claude` 2.1.282 (`-p`, `--output-format json` returns `session_id`, `--resume`, `--allowedTools`, `--permission-mode`, `--max-budget-usd`);
  `codex-cli` 0.155.1 (`exec`, `--json`, `--output-schema`, `exec resume <id>|--last`, `--sandbox`, `-C`);
  `cursor-agent` (`-p`, `--output-format json`, `--resume [chatId]`, `--mode plan|ask`, `--sandbox`).
- Cursor Cloud Agents API (official OpenAPI 1.0.0): `POST /v1/agents` (prompt, repos[].url, startingRef, autoCreatePR=false default),
  `POST /v1/agents/{id}/runs` (follow-up), `GET .../runs/{runId}`, `.../stream` (SSE), `.../cancel`, `/usage`; bearer API key.
  v0 also has `GET /v0/agents/{id}/conversation`. Current Cursor lane commits as `cursoragent@cursor.com` = cloud agent.
- Codex: `@codex <task>` in a PR comment starts a Codex cloud task (official docs) ONLY if Codex cloud is connected to the repo: UNVERIFIED for this repo.
  Current Codex commits use the Founder's local git identity, which indicates local CLI use.
- `LocalCLIAdapter` never passes an instruction and drops stdout (proven by probe). `dispatch_assignment()` has no runtime caller.
- GitHub: NO branch protection and NO rulesets on any branch (incl. default and integration branch).
- Path A provider calls (`providers/registry.chat_with_fallback`) have no spend gate; spend grants apply only to Path B.
- Runtime role can INSERT approval events and INSERT/UPDATE envelopes and spend grants (catalog evidence).

## 1. First real supervised cycle: AI-API -> code -> tests -> GitHub draft PR (Path A)
| # | Change | Exact path |
|---|---|---|
| A1 | Base branch from config (default = Founder-designated integration branch) | `mainai_execution/worktree.py` `BASE_BRANCH`; `execution_job._finalize_repo_edit` `base_branch`; `config.py` |
| A2 | Give the code agent the current files it edits (read from the task worktree; cap size) | `execution_job._handle_repo_edit`, `CODE_AGENT_SYSTEM_PROMPT`; planner names target files |
| A3 | Reject `repo_edit` without `targeted_tests`; feed the failed pytest tail into retry/replan | `planner.create_plan`; `replan.trigger_replan`; `planner.propose_plan_via_ai(goal, failure_context=...)` |
| A4 | Draft PRs | `integrations/github_client.create_pull_request(draft=True)`; `execution_job._handle_open_pr` |
| A5 | Per-goal provider ceiling for Path A (reuse `provider_spend` reserve/settle, do not add a new ledger) | `providers/registry.chat_with_fallback` call sites with `goal_id` |
Acceptance: a Founder-created goal "add a docstring + focused test" produces ONE draft PR against the integration branch;
the remote head equals the local commit (existing check); CI runs; a deliberately failing test causes a retry whose prompt contains the failure;
spend stops at the ceiling; a second run of the same job creates no second PR.

## 2. Smallest working agent communication (outbound + inbound)
Reuse `agent_coordination.adapters.LocalCLIAdapter` + `dispatch.py` + `execution_control.py` (no new framework):
- B1 `adapters.LocalCLIAdapter.start_assignment(assignment_id, instruction)`: append the instruction as the final argv element
  (claude `-p`, codex `exec`, cursor-agent `-p`), always with machine output (`--output-format json` / `--json`).
- B2 `collect_result`: keep bounded stdout (final message + `session_id`/chat id) in `AgentResult` and `output_refs`; parse JSON; never drop content.
- B3 `send_instruction` = resume the SAME session id (`claude --resume`, `codex exec resume <id>`, `cursor-agent --resume <id>`).
- B4 Per-provider safe argv templates in `adapter_config.py`: codex `--sandbox workspace-write -C <worktree>`; claude `--permission-mode` + `--allowedTools` + `--max-budget-usd`;
  cursor-agent `--sandbox enabled`, never `--force/--yolo`. Keep the existing env allowlist and timeout.
- B5 Wire one runtime path: a Path A task type `agent_assignment` whose handler calls `dispatch_assignment` -> `collect_dispatch_result` (existing).
- B6 (cloud, optional second) `integrations/cursor_cloud_client.py` for `/v1/agents` + `/runs` + run status/stream, behind the same adapter Protocol.
Inbound completion signal = the agent's branch/PR on GitHub (truth), plus its final message (report, partial). PR comments to @codex/@cursor: use only after verifying the repo connection.
Acceptance: MainAI launches ONE new local session per agent on a NEW branch/worktree, receives its final message and session id, sends one follow-up
via resume, and the assignment records it. Existing Founder-owned Codex/Cursor sessions are never resumed, interrupted or reassigned.

## 3. Reuse for background reading and prioritization (no new store)
- Documents: `rag` ingestion + claim extraction (wired) -> `project_entities` interpretation proposals -> Founder promote (`/api/project-entities/...`).
- Repo docs/commits/branch-PR status: `project_memory.ingest_doc/ingest_git_commit/ingest_github_status` (`/api/admin/memory/*`, Founder-triggered; add a worker tick).
- Decisions vs proposals: `founder_memory` (explicit decisions, wired in chat), `project_entities` proposal vs promoted entity,
  `work_candidates` (record vs `authorize_work_candidate` -> `create_goal`), `mainai_verification_registry` for verified implementations.
- Prioritization: `work_candidates.priority`, `mainai_vision.gap_generator` (propose only), `autonomous_gap` (inside authorized goals).
- Historical conversations: chat history in `conversations/messages`; Continuous Conversation lane (unmerged) adds canonical memory.

## 4. Parallel tracks that do not touch active lanes
No active lane (cursor sovereignty fix-4 aa0876d, cursor CC fix-4 2d9f62e, codex claim-action fix-4-clean ecf2f29, codex grandmaster 6e08430)
modifies `mainai_execution/*`, `agent_coordination/*`, `integrations/github_client.py`, `providers/registry.py` or `project_memory.py`.
- Track A (A1-A5) and Track B (B1-B5) touch disjoint files and can run in parallel. A4 and Stage-1 GitHub reads both touch `github_client.py`, so give that file one owner.
- Track M (security migration: authority rows Founder-only) touches `alembic/versions` -> waits for the Founder's 0089-0092 integration order
  (current collisions: `0092_cc_one_active_decision` vs `0092_fs_p1_root_of_trust`, both off different 0091s).
- Owners: availability of every builder is UNKNOWN (all lanes have open work). Recommend NOT the Grandmaster lane (competing coordination store).
  The examiner examines each stage and does not build.

## 5. Security controls
Mandatory BEFORE the first supervised cycle:
1. GitHub branch protection or a ruleset on the default + integration branches (no direct push, no force push, PR required). Founder action in GitHub settings.
2. A fine-grained token scoped to this repo only: contents + pull_requests; no admin, no workflow scope.
3. `github_write_enabled` on only for the supervised run; draft PRs only; no merge method (already absent).
4. A Path A spend ceiling (A5) or a provider-side hard budget on the API key.
5. Agent CLIs in sandboxed modes (B4), cwd = assignment worktree, env allowlist, timeout, `--max-budget-usd` where available.
6. The Founder present; the Founder merges manually; the examiner reviews the PR SHA.
Mandatory BEFORE unattended/continuous operation:
7. Authority rows Founder-only: approval events, execution envelopes, spend grants (Track M).
8. Founder credential root of trust closed (sovereignty P1, Cursor fix-4 lane), and the Continuous Conversation P1s.
9. Coordination-store DB guarantees (coordination_v1): unique active worktree/branch, concurrency, fencing, dependencies, before MainAI grants leases herself.
10. A per-goal envelope with cost, time and path ceilings; audit events for every outbound agent instruction; kill switch (workforce epoch exists).

## Dependency order (shortest path)
1. Founder: 5.1 + 5.2, designate the integration branch.          -> unblocks everything
2. Track A (A1-A5)  ||  Track B (B1-B4)                            -> parallel, disjoint files
3. Examiner: examine A and B SHAs.
4. Supervised run #1: MainAI self-edit -> draft PR (Founder merges).
5. B5 wire `agent_assignment`; supervised run #2: MainAI assigns one new task to one agent CLI and reads the result.
6. Stage-1 digest (HANDOFF) on the same reads; MainAI drafts, the Founder approves, MainAI sends via B3.
7. Track M + 5.7-5.10 after the migration order decision -> unattended envelope-scoped operation.
Explicit blockers: Founder GitHub settings (1); migration integration order (Track M); builder availability (UNKNOWN); agent CLI auth (UNKNOWN until a Founder-approved smoke test).

Sources: https://cursor.com/docs-static/cloud-agents-openapi.yaml ; https://docs.cursor.com/en/background-agent/api/agent-conversation ;
https://learn.chatgpt.com/docs/non-interactive-mode ; https://developers.openai.com/codex/integrations/github ; https://docs.claude.com/en/docs/claude-code/headless
