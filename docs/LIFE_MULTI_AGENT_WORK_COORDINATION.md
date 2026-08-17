# LIFE MULTI-AGENT WORK COORDINATION

## WHY THIS EXISTS

Life is increasingly worked on by more than one external coding agent at the same time —
Claude Code, Cursor Agent, Codex, and future CLI/API agents. Today that is coordinated
manually: a founder keeps track, in their own head and in `docs/BRANCH_REGISTRY.md`, of who
is touching what. That does not scale, and it is not something Life itself can reason about.

This foundation gives Life a durable, deterministic record of WHO is working, WHAT they are
working on, WHERE (repository/branch/worktree/path scope), and WITH WHAT AUTHORITY
(read-only vs. read-write), so that two agents can never silently write into the same scope,
duplicate the same work, or bypass authority that was already established elsewhere in the
system.

It is a coordination layer ABOVE the existing canonical work model — never a second one.

## WHAT THIS IS NOT

- Not a second task/job queue. Every `AgentWorkAssignment` references an existing,
  already-authorized `MainAIGoal`/`MainAITask` (`app/models/mainai_execution.py`, migration
  0032). This foundation never mints work; it assigns already-authorized work to an agent.
- Not a second approval gate. `AgentWorkAssignment.approval_required` is descriptive
  coordination metadata about the write this assignment is expected to produce. The real gate
  remains `app.mainai_execution.approval.require_task_approval()`, completely unchanged.
- Not a second capability-learning system. Performance/capability evidence is recorded through
  the existing `app.intelligence_governance` primitives (`IntelligenceExecution`/
  `IntelligenceEvidence`, migration 0038), referenced here by id, never duplicated.
- Not an agent runtime. Nothing in this foundation invokes a real external agent, opens a
  subprocess, makes a network call, or reads/stores a credential. `app.agent_coordination
  .adapters.AgentAdapter` defines only the SHAPE a future, separately-reviewed runtime would
  implement — see "Future Agent Runtime integration" below.
- Not a merge/deploy capability. `AgentAdapter` has no `merge`/`deploy`/`push`/`force_push`/
  `delete_branch` method, structurally, and this module never calls out to git or GitHub.

## THE CONCRETE SCENARIO THIS WAS BUILT AGAINST

Cursor Agent is hardening PR #79's live-autonomy loop
(`backend/app/autonomous_gap/**`, `development_supervisor/**`, `development_driver/**`,
`development_operator/**`, `safe_planner/**`) on `cursor/pr79-live-loop-hardening` (PR #80).
Claude Code needs to review that work read-only, without ever writing into it. Codex should be
free to work a genuinely unrelated area of the repository at the same time. And, when the
founder deliberately wants two agents to compete on the same hard problem in isolated
worktrees, that must be an explicit, intentional act — never an accident.
`tests/backend/mainai/test_multi_agent_work_coordination.py::test_pr79_hardening_scenario_end_to_end`
proves exactly this sequence end to end against a real Postgres database.

## DURABLE MODEL

Five tables (migration 0046):

- **`coordination_agents`** — the provider-neutral agent registry (Claude Code, Cursor Agent,
  Codex, ...). Deliberately NOT owner-scoped/RLS-protected: which coding agents/tools exist is
  founder-wide system knowledge, the same reasoning `EngineeringLesson` already documents for
  itself, not per-owner personal data. Never stores a credential — `model_hint` is an
  informational label ("claude-sonnet-5"), never a key. `register_agent()` is an idempotent
  upsert by `agent_key`, so a restart/reload of whatever process owns the registry converges on
  the same identity instead of accumulating duplicates.
- **`parallel_exploration_groups`** — the explicit, non-accidental form of "two builders on the
  same problem." Groups independent `AgentWorkAssignment`s that are intentionally solving the
  SAME `canonical_problem_ref` in isolated branches/worktrees. Resolution
  (`synthesis_result_ref`) is always filled in by a later, explicit action — this table never
  auto-merges or auto-selects a winner.
- **`agent_work_assignments`** — one durable agent job: WHO (`agent_id`), WHAT
  (`goal_id`/`task_id`), WHERE (`repository_identity`/`branch`/`worktree_path`/
  `allowed_paths`), and with WHAT authority (`read_write_mode`, `risk_level`,
  `approval_required`). `canonical_work_key` is the deterministic identity used for
  duplicate-work detection — a plain function of `(goal_id, task_id, role)`, falling back to
  `(repository_identity, sorted allowed_paths)` when there is no `task_id` yet, never guessed.
- **`agent_work_assignment_dependencies`** — dependency edges, mirroring
  `mainai_task_dependencies`'s exact shape. A dependent (typically a REVIEWER) stays
  `waiting_dependency` until every dependency reaches a releasing status
  (`ready_for_review`/`verified`/`completed`).
- **`agent_scope_leases`** — the deterministic write-conflict-prevention primitive.
  `lease_generation` follows `mainai_jobs.lease_generation`'s exact fencing convention: bumped
  by exactly 1 on every takeover, IN PLACE on the same row, never a new row — a caller must
  present the current generation to mutate/renew/release, never worker/agent identity alone.
  Staleness is always an `expires_at` timestamp comparison at read time, never a separate
  stored status value, matching `mainai_jobs.lease_expires_at`.
- **`agent_work_assignment_events`** — append-only history, mirroring `mainai_task_events`'s
  exact shape: a `BEFORE UPDATE OR DELETE` trigger enforces INSERT-only at the DB level
  regardless of the calling role's raw grants, and `erase_own_agent_coordination_children()`
  (SECURITY DEFINER, no caller-supplied owner argument — the same shape
  `erase_own_mainai_job_children()` already establishes) is the one authorized deletion path,
  wired into `erase_account_data()`.

## DETERMINISTIC CONFLICT DETECTION

Everything the coordinator decides is a pure function over what Life already knows — never
natural-language reasoning about whether two scopes "probably" overlap.

**Path-prefix conflict** (`app.agent_coordination.service.paths_conflict`/
`paths_conflict_any`) — two path prefixes conflict iff one is an ancestor-or-equal of the
other, directory-boundary-aware (`backend/app/dev` is never treated as overlapping
`backend/app/development_x/...` merely because the raw strings share a prefix). An empty scope
never conflicts with anything — a read-only reviewer with `allowed_paths=[]` can always
overlap a builder's write scope.

**The coordinator** (`evaluate_assignment_readiness`) answers "can this assignment run right
now" by checking, in order: agent availability/concurrency, whether the assignment's recorded
authority (`goal.original_instruction`'s hash) still matches what the goal currently says,
whether its recorded `base_sha` is still current, whether its dependencies are satisfied,
whether it duplicates another non-terminal assignment's canonical work, and — for a
`read_write` assignment — whether any other active lease on the same repository shares its
branch/worktree (`WORKTREE_CONFLICT`) or overlaps its path scope (`PATH_CONFLICT`), and
whether it holds an active lease at all (`LEASE_REQUIRED`).

**Scope leases** (`acquire_lease`/`renew_lease`/`take_over_lease`/`release_lease`) are the
actual write-conflict gate. READ_ONLY scopes skip conflict scanning entirely — rule 1. A
`read_write` acquisition takes a `SELECT ... FOR UPDATE` over every other active lease on the
same `repository_identity` before deciding, so two concurrent acquisitions can never both
succeed against overlapping scope. Re-acquiring for the SAME assignment with a subset of its
already-held scope replays idempotently; requesting a WIDER scope is rejected
(`ScopeExpansionError`) — a worker may never silently expand its own path scope. A stale
lease (past its own `expires_at`) can be taken over by a new agent; a live lease can only ever
be released by its own holder. `expected_generation` makes a concurrent double-takeover race
deterministic: the loser's expectation no longer matches the winner's already-applied bump and
is rejected, not silently re-applied.

**Parallel exploration is the one, explicit exemption from `PATH_CONFLICT`** — two assignments
sharing a `parallel_exploration_group_id` may overlap path scope. It is never an exemption from
`WORKTREE_CONFLICT`: two agents may never mutate the same physical worktree, even when they are
intentionally competing on the same problem.

## CAPABILITY / PERFORMANCE EVIDENCE

`record_assignment_execution`/`record_assignment_outcome` are thin adapters over
`app.intelligence_governance.record_execution`/`record_evidence` — the ONLY place
capability/performance evidence enters this module's own tables, as a foreign-key reference on
`AgentWorkAssignment.intelligence_execution_id`, never a duplicated column. Recording evidence
never mutates the assignment's own status, role, or path scope — evidence attachment and
canonical work truth are kept strictly separate. Both calls are idempotent by construction:
calling `record_assignment_execution` twice for the same assignment returns the same
`IntelligenceExecution` row.

## FUTURE AGENT RUNTIME INTEGRATION (explicitly deferred)

`app.agent_coordination.adapters.AgentAdapter` is a `typing.Protocol` (not a base class
instances are required to inherit from), the same pattern
`app.provider_planning.service.PlanningAdapter` already establishes for provider-assisted
planning. A concrete implementation — actually driving Claude Code/Cursor Agent/Codex CLIs or
APIs against an assignment — is deliberately out of scope for this foundation PR. It would need
its own, separately reviewed PR to add: process/session management, credential handling
(outside this codebase entirely), output streaming, and cancellation semantics. None of that
belongs in a coordination-layer foundation whose entire job is bookkeeping and conflict
prevention, not execution.

## EXPLICITLY DEFERRED

- Actually invoking a real external agent (see "Future Agent Runtime integration").
- Automatic synthesis/selection of a winner across a parallel-exploration group — resolution is
  always a later, explicit action.
- Automatic merge, deploy, or push of any kind.
- A UI surface for founders to browse assignments/leases (this foundation is data + service
  layer only, matching every other "foundation" layer in this codebase — see
  `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §8's build order).
- Widening `intelligence_executions.role`'s CHECK-constrained vocabulary to natively cover this
  module's richer role set (TESTER/RESEARCHER/SYNTHESIZER map to their closest existing bucket
  for that table; this module's own, real role is preserved verbatim in that execution's
  `context`, which is unconstrained JSON).
