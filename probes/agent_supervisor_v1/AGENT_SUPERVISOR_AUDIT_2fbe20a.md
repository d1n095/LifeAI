# MainAI agent-supervisor integration audit (read-only)

Audited SHA: 2fbe20aacf1203fc0e16d216ef55b666b2181619 (`codex/founder-alpha-final-composed-candidate`).
In-flight reuse target also read: 2d9f62e (Continuous Conversation P1-fix-4, RETURN_TO_BUILDER, unmerged).
Date: 2026-10-11. Nothing modified; no agent invoked, interrupted or reassigned.

## Verified evidence
- `probe_cli_adapter_io.py` (with `/bin/echo` as a stand-in executable): `LocalCLIAdapter.start_assignment(assignment_id)` takes no instruction,
  and `send_instruction` raises NotImplementedError. `collect_result` returns only `{stdout_bytes: 16}` and drops the content.
- 137 existing agent-layer tests pass (dispatch foundation, execution control, real-execution bridge, runtime view, multi-agent coordination);
  they use fake adapters.
- `dispatch_assignment()` has no runtime caller (no route or worker); the only runtime use of agent_coordination is `worker.expire_stale_active_leases`.
- GitHub reality (read-only API): Cursor commits are authored by `Cursor Agent <cursoragent@cursor.com>`. Codex commits use the Founder's local git identity
  and are distinguishable only by the `codex/` prefix. Agent reports live in draft-PR bodies (#253/#260/#261/#262: 2.7k-4.4k chars).
  The only PR comments are from Vercel. Instructions currently flow only through the Founder.

## Assessment
| Capability | Verdict | Code path / evidence |
|---|---|---|
| Read agent results via GitHub | PARTIAL | github_client: get_ref, get_pull_request (body = agent report), list_pull_requests_for_head, list_check_runs (conclusion/output summary, not paginated). MISSING: list branches/PRs, PR comments, file contents, CI logs. |
| Read agent results via CLI adapter | MISSING | adapters.LocalCLIAdapter.collect_result discards stdout (proven). |
| Send instructions to agents | MISSING | No instruction parameter; send_instruction NotImplemented; no PR-comment write; no Cursor/Codex API client. |
| Track assignment/branch/worktree/status/blockers/SHA | PARTIAL | agent_work_assignments (branch, worktree_path, head_sha, upstream_pr_ref, status), runtime_view.work_registry_snapshot / all_agents_runtime_snapshot, bootstrap.bootstrap_known_agents. Not populated, not synced from GitHub; DB-level guarantees weak (see coordination_v1). |
| Connect to Path A | PARTIAL | POST /goals, plan, read_only_audit task_type, worker dispatch exist; _handle_read_only_audit sends only task.description to the model (no context injection). |
| Inspect repo files | PARTIAL | development_operator read_file/search_text/repo_diff (local, envelope-governed). GitHub contents MISSING; Path A agents never receive file contents. |
| Inspect test failures | PARTIAL | Path A stores pytest stdout_tail/stderr_tail in task events; not fed back to any model. |
| Inspect CI | PARTIAL | ci_wait + list_check_runs (conclusions). No logs/annotations. |
| Inspect examiner verdicts | PARTIAL | mainai_verification_registry (privileged insert, readable); examiner REPORTS.md on examiner branches is a machine-readable table; no ingestion. |

Coordination systems that must NOT be extended in parallel: agent_orchestration.py (/api/agents, AI-API code/review agents), agent_coordination,
mainai_execution Path A, development_supervisor Path B, workforce/mainai_workforce, dev_director, unmerged Grandmaster SQLite and Ledger.
Reuse: agent_coordination tables as runtime state, GitHub as versioned truth, Path A as the executor, CC occupancy (already composes runtime_view).

## Smallest set of changes before MainAI can do the coordination work (dependency order)
Stage 1 - first supervised agent-message cycle (observe, understand, propose; the Founder relays):
1. GitHubClient read additions (same client): list branches by prefix, list open PRs, PR body/comments, file contents at ref, paginated check runs.
2. Populate existing agent_work_assignments for current lanes via create_work_assignment (branch, upstream_pr_ref);
   a read-only observer records GitHub-observed head SHA + observed_at in head_sha/output_refs (no new table).
3. Supervisor digest on Path A: a read_only_audit task whose prompt is a deterministic context bundle (runtime_view snapshot,
   GitHub tips/PR bodies/check conclusions, latest examiner verdict rows). Output: per-agent status, blockers and a DRAFT next
   instruction per agent. The Founder approves and relays it. No agent is interrupted.
   Done when: the digest matches GitHub for every lane and one instruction has been relayed by the Founder.
Stage 2 - first MainAI-generated draft PR (blockers from READINESS_2fbe20a.md): configurable BASE_BRANCH, file contents to the code agent,
   non-empty test plan + failure feedback, draft=True PRs, examiner verdict before ready-for-review.
Stage 3 - continuous coordination within Founder permissions:
   a periodic envelope-scoped digest tick; an outbound channel for Founder-approved instructions (PR comment, or LocalCLIAdapter passing
   the instruction and capturing bounded stdout for local Claude Code); authority rows (approvals/envelopes/spend) made Founder-only;
   coordination_v1 DB guarantees before MainAI grants leases itself.

## Assumptions (not verified)
- Cursor/Codex cloud agents act on @-mentions in PR comments: unverified; until verified, the Founder remains the relay.
- An API exists for programmatically assigning work to Cursor/Codex: unverified; out of scope for Stage 1.
