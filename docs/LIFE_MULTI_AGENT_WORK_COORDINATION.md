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

## RUNTIME VISIBILITY & DETERMINISTIC ROUTING

Extends the foundation above with a read-model and an eligibility-filter layer — no new
tables, no migration. Both are pure functions over the five tables migration 0046 already
created; nothing here is cached, stored, or duplicated.

**`app.agent_coordination.runtime_view`** answers "what is every registered agent doing right
now" and "who is doing what, where, with what authority" as a single deterministic snapshot.
`RuntimeStatus` (`IDLE`/`RUNNING`/`WAITING_DEPENDENCY`/`WAITING_REVIEW`/`REVIEWING`/`BLOCKED`/
`COMPLETED`/`FAILED`/`OFFLINE`) is a coarser, founder-facing VIEW derived by a deterministic,
total mapping from the canonical `WorkAssignmentStatus` — never a replacement for it; every
`AssignmentRuntimeView` carries the underlying canonical status verbatim alongside the mapped
one. An agent's overall `runtime_status` is the highest-priority status across its current
non-terminal assignments (`RUNNING > REVIEWING > WAITING_REVIEW > WAITING_DEPENDENCY >
BLOCKED`, else `IDLE`); a registry-disabled/unavailable agent always reports `OFFLINE`,
overriding even an assignment still mid-flight underneath it — disabling an agent is the
founder's own emergency stop and must never be masked as "business as usual." A block/wait
reason is never stored; it is the LIVE `CoordinatorDecision` from
`evaluate_assignment_readiness()`, so it can never drift from what that function would say if
asked directly. `agent_runtime_snapshot()`/`work_registry_snapshot()` both take an explicit
`owner_id` and filter `agent_work_assignments` by it directly — defense in depth alongside RLS,
matching this codebase's established doctrine — even though `coordination_agents` itself stays
deliberately founder-wide and unfiltered.

For an assignment `evaluate_assignment_readiness()` would itself call `ASSIGNABLE` (no
structural dependency/duplicate/scope/staleness/availability blocker) but whose canonical
status is the explicit, human/process-set `blocked` or `changes_requested` — reached only
through a direct `transition_status()` call, never computed by that function — `block_reason`
falls back to the most recent `status_changed` event's own `detail`, the same append-only log
every other piece of an assignment's history already goes through, rather than silently
reporting no reason at all for a row someone explicitly marked blocked. This is what actually
lets a caller distinguish "waiting for review" from "waiting for approval/a provider/an
external resource/a branch-PR/a founder decision/truly impossible" — the founder's own
requested wait-reason vocabulary — using data this module already writes: any caller blocking
an assignment records `detail={"reason": "waiting_founder_decision"}` (or whatever taxonomy
string it wants) and the runtime view surfaces exactly that, verbatim, never invented here.

**`app.agent_coordination.routing`** answers "which currently registered agent(s) are eligible
for this assignment" as a pure filter, in this fixed order: the reviewer/researcher-must-be-
read_only invariant, a scope-conflict pre-check (`scan_write_scope_conflict()` — a refactor
extracted from `evaluate_assignment_readiness()`'s own conflict-scan so both the existing
per-assignment check and this pre-assignment eligibility check share ONE implementation,
skipped entirely for `read_only` exactly like `acquire_lease()`'s own "rule 1"), then per
candidate: registry status, read/write capability, required capability tags, and availability
(`agent_availability()`). Every rejection is recorded with its concrete reason, never silently
dropped. This is eligibility FILTERING, not selection authority — performance evidence never
enters this decision (there isn't yet enough of it to be meaningful; see "Explicitly deferred"
below), and ties return `NEEDS_SELECTION` rather than guessing a winner. Provider identity or a
"trusted-sounding" agent name grants nothing here either, the same invariant
`test_provider_identity_cannot_grant_authority` already proves at the assignment layer.

**`app.agent_coordination.routing.next_feasible_assignment_for_agent`** answers the INVERSE
question — "given a known agent (typically one that just went idle), which of its OWN
already-assigned `ready` work should it pick up next" — the complementary direction to
`eligible_agents_for()`'s "given work, which agent." Strict FIFO by `created_at`; no priority
or capability-fit scoring (that would be exactly the "ranking engine built on insufficient
evidence" this module already refuses to build). Scans in order and returns the first
assignment `evaluate_assignment_readiness()` calls `ASSIGNABLE` **or** `LEASE_REQUIRED` —
`LEASE_REQUIRED` is deliberately treated as "found," not "skip," since a fresh `read_write`
`ready` assignment always reports it until a lease is acquired; that is simply naming the
caller's own very next step, not a real blocker. A genuinely stuck assignment ahead in the
queue (stale base, a surfaced duplicate, agent unavailability) never disqualifies a feasible
one behind it — every skip is recorded with its real `CoordinatorDecision.outcome`, never
silently dropped. This is what lets "one blocked assignment must not freeze unrelated work"
hold from an individual AGENT's own point of view, not just the coordinator's. Never mutates
anything — selecting a next assignment and actually starting it (`acquire_lease()` +
`transition_status()`) remain separate, deliberate caller actions.

**`app.agent_coordination.routing.idle_agents_with_next_assignment`** is the single-call
composition of the two directions above with `runtime_view`'s own snapshot — "who is free
right now, and what should each of them do next," exactly what a founder (or a future
orchestration loop under founder-set policy) needs to actually put an idle agent back to work
without querying the registry and then the selector separately for every agent by hand. Only
truly `RuntimeStatus.IDLE` agents are included — `RUNNING`/`REVIEWING`/`BLOCKED`/`OFFLINE`/etc.
are busy or unavailable, not "idle with nothing to do," and are correctly omitted rather than
included with a hollow decision. An idle agent CAN still appear with
`NO_FEASIBLE_ASSIGNMENT` — that is itself meaningful (genuinely free, genuinely nothing to
give it), distinct from not appearing at all. A pure composition, never a new data source or
decision rule of its own.

**`app.agent_coordination.service.build_agent_outcome_payload`** is a canonical, documented
field vocabulary (tests/duration/cost/CI outcome/review defects/severity/rework/scope
violations/merge result/failure reason/verified quality — every field optional, omitted
entirely when not supplied) for `record_assignment_outcome()`'s `payload` argument. It is a
plain dict, not a new store — `IntelligenceEvidence.payload` is already unconstrained JSON —
existing purely so every caller records outcome evidence using the SAME field names instead of
each inventing its own ad hoc shape, which is what a future, evidence-driven Agent Capability
Matrix would need to read consistently once there is enough accumulated evidence to be
meaningful.

`tests/backend/mainai/test_agent_runtime_control_plane.py`'s
`test_current_real_world_three_agent_state_end_to_end` proves this layer end to end against the
concrete situation it exists to represent: Cursor Agent `RUNNING`/`WRITE` on PR #80's live-loop
paths, Claude Code `RUNNING`/`WRITE` on this very coordination module (a genuinely different,
non-overlapping subsystem), Codex `IDLE` — then an overlapping Claude/Codex write request
against Cursor's scope refused, a non-overlapping one allowed, a dependent reviewer released
into `REVIEWING`, a Claude assignment gated on Cursor's completion correctly reported
`WAITING_DEPENDENCY` while Cursor's own work continues unaffected, and routing still resolving
normally for other feasible work despite that one blocked assignment.

## BOUNDED DISPATCH FOUNDATION

Turns a routing decision ("agent X should do assignment Y next") into a real, auditable
dispatch, without granting any authority the coordination layer did not already, explicitly
grant. Three pieces, all pure reuse — no new tables, no new registry, no new task/job/approval
system.

**`app.agent_coordination.bootstrap.bootstrap_known_agents`** idempotently registers Life's
actual, currently-used worker identities (Claude Code, Cursor Agent, Codex) via
`register_agent()`'s own upsert-by-`agent_key` — never a new registry. Represents
identity/capability/config only; never a credential, secret, or machine-specific token (nothing
here reads an environment variable or a secrets store). Capabilities are the conservative,
currently-known-true shape of each agent's role — interactive CLI-driven repo editing,
read-only review, running tests — never an invented performance ranking. Deliberately NOT
wired into automatic app boot: seeding actual founder-facing data about which real agents
exist is a decision, not mechanical infrastructure, and stays an explicit, callable action.

**`app.agent_coordination.dispatch.DISPATCH_LIFECYCLE`** maps the founder's own requested
naming (PROPOSED/READY/DISPATCHING/RUNNING/COMPLETED/FAILED/CANCELLED/BLOCKED) onto the
ALREADY EXISTING `WorkAssignmentStatus` — `DISPATCHING` reuses `waiting_agent` (ready,
allocated, not yet confirmed started — exactly what that status has always meant), never a
new column. "AUTHORIZED" has no status of its own: it is exactly "ready AND passes
`evaluate_dispatch_readiness()`'s approval check," computed at read time, never stored — the
same "derived, never stored" doctrine `runtime_view`'s own block-reason handling already
establishes.

**`evaluate_dispatch_readiness()`** is the fail-closed gate immediately before any real
invocation. Layers strictly on top of `evaluate_assignment_readiness()` (which alone must
report `ASSIGNABLE`, not merely `LEASE_REQUIRED` — a dispatch is about to actually invoke a
real agent and must already hold its write lease, unlike `next_feasible_assignment_for_agent`'s
own selection-time tolerance for that outcome), then adds: capability match, an explicit
branch+worktree for any `read_write` dispatch, and founder approval — delegated entirely to
`app.mainai_execution.approval.require_task_approval()`, the real gate, never reimplemented.
When an assignment has no linked `task_id` but `approval_required` is set, there is no real
gate to check against yet — fails closed rather than treating "nothing to check" as "approved."

**`dispatch_assignment(db, assignment=, agent=, adapter=, authority_envelope=)`** is the
`dispatch(agent_id, assignment_id, authority_envelope)` control-plane entry point. Always
re-runs `evaluate_dispatch_readiness()` immediately before touching the adapter — on failure,
the adapter is never called and nothing is mutated. `authority_envelope`, if supplied, is
validated to be a subset of the assignment's own already-narrowed `allowed_paths` — an adapter
implementation is never trusted to self-limit; this function enforces the boundary itself.
Transitions `ready -> waiting_agent` (DISPATCHING) before calling the adapter, and only to
`running` after `adapter.start_assignment()` returns without raising. On
`ProviderNotConfiguredError`, transitions to `blocked` with a structured
`REAL_PROVIDER_NOT_CONFIGURED` reason — never silently reports success, never leaves an
assignment looking like it is running when nothing real happened.

**`app.agent_coordination.adapters.NotConfiguredAdapter`** is the REAL default `AgentAdapter`
for every provider until a genuine, separately-reviewed Agent Runtime exists — it implements
the full Protocol shape but every method that would touch an external agent raises
`ProviderNotConfiguredError`. It opens no subprocess, makes no network call, and reads no
credential, by construction.

**`DispatchResult`/`apply_dispatch_result()`** is the structured result handoff — base/head
sha, branch, worktree, changed paths, tests, CI refs, PR ref, duration/cost, all optional and
recorded verbatim, never interpreted into a fabricated quality score. Recorded through the
EXISTING `record_assignment_execution()`/`record_assignment_outcome()`/
`build_agent_outcome_payload()` primitives PR #83 already built, and transitioned through the
EXISTING state machine (`transition_status()`) — never a raw status write, never a second
evidence store.

`tests/backend/mainai/test_agent_dispatch_foundation.py`'s
`test_current_real_world_dispatch_scenario_end_to_end` proves the whole chain against the
concrete situation this foundation exists to represent: Cursor busy on PR #79/#80's exact
paths, Claude free after PR #84, Codex idle — Life sees the overlap refused, selects Claude for
a genuinely unrelated task, creates the dispatch (a real `AgentWorkAssignment`, never a second
representation), refuses a colliding dispatch attempt again at the gate (defense in depth, not
just at routing time), dispatches the non-overlapping one through a fake adapter (no real
provider configured yet), and records its result — all without the assignment's own
`allowed_paths` ever changing from what was granted at creation.

## REAL AGENT EXECUTION BRIDGE

Moves dispatch from "create a bounded dispatch record" to "actually invoke a real configured
local CLI agent" — without inventing a credential, without silently widening authority, and
without ever faking a successful external run. Two pieces, both additive to the dispatch
foundation above — no new tables, no second adapter registry.

**`app.agent_coordination.adapter_config`** is the founder-controlled enablement boundary,
five DISTINCT facts about a provider, never conflated: `supported` (this codebase has a real
adapter registered for the key — a code-level fact, true regardless of the local machine),
`executable_found` (`shutil.which()` found a binary on THIS machine's PATH — detection only,
never itself an authorization to invoke it), `credentials_state` (always `"unknown"` unless the
founder explicitly asserts `"configured"` via
`LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__<KEY>` — this module never inspects auth files,
never runs a status subcommand, never guesses), `enabled` (the founder's own explicit opt-in,
`LIFE_AGENT_ADAPTER_ENABLED__<KEY>=true`, defaulting to `False` — the ONLY thing that turns
"code exists" into "code may run"), and `dispatch_authorized` (computed separately, per
assignment, by `evaluate_dispatch_readiness()` below — out of this module's scope entirely).
`real_adapter_config()` returns a real `(executable, args_template, timeout_seconds)` tuple
ONLY when `enabled=True` AND the executable is genuinely found AND the founder has supplied an
explicit `LIFE_AGENT_ADAPTER_ARGS__<KEY>` invocation template — this module never invents CLI
flags for Claude Code/Cursor Agent/Codex. No environment variable this module reads is ever a
credential, secret, or session token — only plain boolean/string configuration flags.

**`app.agent_coordination.adapters.LocalCLIAdapter`** is the ONE bounded, provider-neutral real
`AgentAdapter` implementation — the SAME subprocess mechanism serves Claude Code, Cursor Agent,
or Codex, parametrized entirely by `adapter_config`'s own output, never a per-provider
duplicated implementation. Every invocation is bounded by construction: the assignment's own
`worktree_path` as an exact `cwd` (never inferred, never this process's own cwd); a real,
always-present `timeout_seconds` enforced by `asyncio.wait_for()`, with the process killed on
expiry; list-form `argv` via `asyncio.create_subprocess_exec()` — never `shell=True`, no code
path capable of unrestricted shell passthrough; a minimized, caller-supplied `env`, never a
blind inheritance of this process's own full environment. `send_instruction()`/`resume()` are
deliberately `NotImplementedError` — this is a bounded, single-shot, non-interactive invocation
(start, wait up to the bound, capture the result), not an interactive session this coordination
layer would have to trust mid-flight. `get_real_adapter(provider_key, cwd=, env=)` is the
founder-controlled factory: returns a real `LocalCLIAdapter` only when `real_adapter_config()`
confirms every precondition, `NotConfiguredAdapter` — the same honest, fail-closed default — in
every other case; never silently substitutes a fake/mock adapter.

Two new, honest failure signals, distinct from an ordinary non-zero exit (`AgentResult
.succeeded=False`) and distinct from `ProviderNotConfiguredError` ("never even tried"):
`AdapterProcessLostError` (the subprocess could not be started, or was being tracked and is no
longer traceable) and `AdapterTimeoutError` (the subprocess exceeded its configured bound and
was killed before this is raised). Life must never read either of these as "completed."

**`evaluate_dispatch_readiness(..., require_adapter_enabled=True)`** is an opt-in extra gate
check (default `False`) — a caller intending to dispatch through a REAL adapter passes `True`
to also fail closed on `adapter_availability()` for this exact agent: `ADAPTER_DISABLED` when
the founder has not explicitly enabled it, `ADAPTER_UNAVAILABLE` when enabled but the
executable was not found. Default `False` deliberately does NOT check this — a caller
intentionally dispatching through a test-only fake adapter is never forced to also satisfy
real-adapter configuration that has nothing to do with what it is actually about to call.
`dispatch_assignment()` distinguishes, never conflates, every crash mode on the START side —
`ProviderNotConfiguredError`/`AdapterProcessLostError`/`AdapterTimeoutError`/any other
unanticipated exception — each transitions the assignment to `blocked` with its own specific
structured reason (never left stuck in `waiting_agent`, which would silently read as "idle" in
`runtime_view`) before propagating. A fresh `attempt_id` is generated for every genuine
invocation attempt and returned on the `DispatchDecision`, so a caller can correlate a specific
attempt with its eventual result even if the same assignment is retried after a failure.

**`collect_dispatch_result()`** is the companion on the COLLECTION side — calls
`adapter.collect_result()`, distinguishes the same `AdapterProcessLostError`/
`AdapterTimeoutError` (a process that disappeared or ran over its bound AFTER having started is
exactly as real a failure as one that never started), and otherwise applies the observed
`AgentResult` through the EXISTING `apply_dispatch_result()`. `DispatchResult` gained
`adapter_key`/`dispatch_attempt_id` — which real provider (or `"fake"`/`"not_configured"`)
actually produced a given result, and which specific attempt it correlates with — merged into
the existing evidence payload, never a second evidence store.

`tests/backend/mainai/test_agent_real_execution_bridge.py` proves the subprocess MECHANISM
against harmless, already-installed system binaries (`/bin/echo`, a nonexistent path, a
deliberately-short-timeout `sleep`) — never against a real coding agent CLI; every real-agent
adapter stays disabled by default, verified directly against the actual local machine
(`test_no_real_provider_is_enabled_by_default`). Its own
`test_current_real_world_dispatch_scenario_with_full_gate_coverage` extends the same concrete
Cursor-busy/Claude-free/Codex-idle scenario `test_agent_dispatch_foundation.py` already proves,
additionally exercising every individual gate rejection (path conflict, wrong worktree, missing
approval, disabled adapter, unavailable adapter, stale base) against real coordination state,
with the actual dispatch progression still going through the deterministic fake adapter
(explicitly sanctioned for automated tests) — no real Claude Code/Cursor Agent/Codex invocation
happens anywhere in this branch's own code paths or tests.

## INTERACTIVE AGENT EXECUTION CONTROL

Extends the start-then-collect dispatch foundation above into a provider-neutral model that
can track a REAL, long-running agent process/session: output arriving over time, heartbeats,
an interactive control contract (instruction/status/cancel/resume), and honest
reconnect/recovery semantics — without inventing a second supervisor and without ever
fabricating a `completed` status. One new table (migration 0047,
`agent_dispatch_executions`) plus one new event-type value on the EXISTING
`agent_work_assignment_events` table (`execution_observed`, a plain CHECK-constraint addition
— not a native Postgres enum, so no `ALTER TYPE` was needed); no second evidence store, no
second dispatch gate.

**`app.agent_coordination.execution_control`** is a layer ABOVE `dispatch.py`, never a
replacement: every function either calls `dispatch.py`'s own already-reviewed functions
(`dispatch_assignment`, `collect_dispatch_result`) or reads/writes the new
`AgentDispatchExecution` tracking row that `dispatch.py` itself never touches.
`WorkAssignmentStatus` (the canonical, coarser state machine `transition_status()` enforces)
remains the single source of truth for an assignment's own lifecycle; `ExecutionAdapterState`
(`starting`/`running`/`exited`/`lost`/`timeout`/`cancelled`) tracks the finer-grained,
per-ATTEMPT process/session state underneath it — the two are kept in sync by this module's
own functions, never left to drift independently.

**Output streaming** (`ExecutionEvent`/`record_execution_event()`): structured event kinds
(`status`/`progress`/`tool_action`/`heartbeat`/`partial_result`/`final_result`) are ALWAYS
persisted as a durable `execution_observed` `AgentWorkAssignmentEvent` — the same
append-only, RLS-protected, trigger-enforced history every other assignment event already
goes through. Raw `stdout`/`stderr` is, by default, NOT persisted this way — only its ARRIVAL
TIME updates `AgentDispatchExecution.last_output_at` — because a real CLI agent's raw output
can be arbitrarily large and high-frequency; a caller may pass `persist=True` explicitly for
a specific chunk it wants durable, an opt-in exception, never the default. Does not pretend
every provider exposes the same fidelity — an event `kind` this module does not recognize is
still accepted, just treated as ephemeral unless the caller forces it durable.

**Interactive control contract** (`AdapterCapabilities`, `send_execution_instruction()`/
`request_execution_status()`/`cancel_execution()`/`resume_execution()`): every adapter
declares its own truthful `control_capabilities()` — `supports_streaming`/
`supports_instruction`/`supports_resume`/`supports_cancel`/`supports_structured_events`.
`send_execution_instruction()`/`cancel_execution()`/`resume_execution()` check the relevant
flag BEFORE ever calling the adapter's own method — an unsupported operation returns a
structured `ControlOutcome(OUTCOME_UNSUPPORTED_CAPABILITY, ...)`, never a raised
`NotImplementedError` a caller has to specifically catch, and never a silent no-op.
`request_execution_status()` is the one exception — `observe()` is part of the BASE
`AgentAdapter` Protocol, not capability-gated (there is no `supports_status` flag by design);
its own honest refusal (e.g. `ProviderNotConfiguredError`) still propagates straight through.
`LocalCLIAdapter` truthfully declares only `supports_cancel=True` (a real `SIGTERM` against
its own tracked, still-running process) — single-shot/non-interactive by construction, exactly
as its own docstring already said; `NotConfiguredAdapter` declares every flag `False`.

**Long-running process tracking** (`AgentDispatchExecution`, migration 0047): one live,
mutable row per dispatch ATTEMPT — distinct from `AgentWorkAssignmentEvent` (append-only
history) the same way `AgentScopeLease` is distinct from `AgentWorkAssignment`. Correlates
with `dispatch.DispatchDecision.attempt_id`; `start_execution_tracking()` is called only after
`dispatch_assignment()` returns its actual `OUTCOME_ASSIGNABLE` success outcome — `attempt_id`
itself is generated BEFORE `adapter.start_assignment()` is even attempted (so a crashed start
still correlates its own `blocked` transition with a specific attempt id), but nothing gets a
tracking row unless something genuinely started running. `process_ref` is an opaque,
adapter-supplied identifier (a PID, a session token STRING) — informational/correlation only,
never a credential, exactly like `CoordinationAgent.model_hint`.

**Reconnect / recovery** (`reconcile_execution_state()`): observes and classifies only —
process still alive / exited-but-not-yet-ingested / exited-and-already-ingested / adapter
disconnected (never configured) / session lost (a tracked process no longer traceable, e.g.
after a backend restart) / result unavailable (never actually started). NEVER changes
`AgentWorkAssignment.status` itself — only `collect_dispatch_result()` (via
`apply_dispatch_result()`) is ever allowed to advance an assignment to `completed`, exactly
like `evaluate_dispatch_readiness()` never dispatches anything itself. No false COMPLETE is
possible through this function by construction.

**Collection-side wrapper** (`collect_and_ingest_execution_result()`): calls
`dispatch.collect_dispatch_result()` (never reimplements its own crash handling) and mirrors
the outcome onto the tracking row — `adapter_state`/`result_ingestion_status` set to
`lost`/`timeout`/`failed` on a crash, `exited`/`ingested` on success. If
`collect_dispatch_result()` itself raises (a genuine "agent completed but result ingestion
failed" case), marks `result_ingestion_status=failed` and re-raises — the caller's own
transaction boundary still decides what happens next.

**Founder-controlled credential/config interface** (`adapter_config.credential_reference()`/
`resolve_adapter_env()`): `credential_reference(provider_key)` returns an opaque, founder-
supplied LABEL (e.g. `"vault:codex-oauth"`) via `LIFE_AGENT_ADAPTER_CREDENTIAL_REF__<KEY>` —
never a secret; `None` means "unresolved / config-required," since this codebase has no
secret-storage backend of any kind. `resolve_adapter_env(provider_key)` selectively forwards
ONLY the ambient environment variable NAMES the founder explicitly allowlists via
`LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__<KEY>` (comma-separated names, never values) — never a
blind inheritance of the whole process environment; `get_real_adapter()` now defaults to this
allowlist whenever a caller omits `env` explicitly (passing `env={}` still means "nothing at
all," unchanged from before).

`tests/backend/mainai/test_agent_execution_control.py` proves the full control loop using a
deterministic, fully-capable fake adapter (`_FakeStreamingAgentAdapter`, clearly labelled,
never in production code) for the streaming/instruction/cancel/resume path, and
`LocalCLIAdapter` against harmless, already-installed system binaries (`/usr/bin/true`,
`/bin/sleep`, a nonexistent path) for the lost-process and timeout paths — a genuinely real
subprocess mechanism, never a real coding-agent CLI. No real Claude Code/Cursor Agent/Codex
invocation happens anywhere in this file.

## FUTURE AGENT RUNTIME INTEGRATION (still explicitly deferred)

`app.agent_coordination.adapters.AgentAdapter` is a `typing.Protocol` (not a base class
instances are required to inherit from), the same pattern
`app.provider_planning.service.PlanningAdapter` already establishes for provider-assisted
planning. `LocalCLIAdapter` above is the one bounded, provider-neutral REAL mechanism this
codebase implements — but it remains fully inert for every real provider until the founder
explicitly supplies `LIFE_AGENT_ADAPTER_ENABLED__<KEY>=true` AND a real
`LIFE_AGENT_ADAPTER_ARGS__<KEY>` invocation template; `NotConfiguredAdapter` stays the honest,
fail-closed default until then. The provider-neutral output-streaming/interactive-control/
reconnect MECHANISM now exists (see "Interactive Agent Execution Control" above) and
`LocalCLIAdapter` truthfully participates in it (declaring only `supports_cancel=True`) — but
no REAL provider today declares `supports_streaming`/`supports_instruction`/`supports_resume`;
that would require either a genuinely interactive real adapter implementation (a future,
separately-reviewed PR) or a provider CLI that itself exposes a resumable session this
codebase could drive. Credential handling remains entirely outside this codebase — this
module never reads, stores, or references a secret, only an opaque reference LABEL (see
`credential_reference()` above). None of that belongs in a coordination-layer foundation whose
entire job is bookkeeping, conflict prevention, and bounded dispatch orchestration — never
unbounded or unattended execution.

## EXPLICITLY DEFERRED

- Actually enabling a real external agent for genuine use (mechanism exists via
  `LocalCLIAdapter`; see "Real Agent Execution Bridge" — every provider stays disabled until the
  founder explicitly configures it).
- A REAL provider adapter that declares `supports_streaming`/`supports_instruction`/
  `supports_resume=True` — the provider-neutral mechanism exists (see "Interactive Agent
  Execution Control"), but no current adapter implementation is genuinely interactive;
  `LocalCLIAdapter` stays single-shot/non-interactive by construction.
- Actual secret storage/resolution for `credential_reference()`'s own opaque labels — this
  module represents the reference boundary only; resolving a label into a usable credential is
  a distinct, not-yet-built subsystem this module deliberately does not reach into.
- Output streaming, interactive mid-run instructions, and provider-specific invocation shapes
  beyond a founder-supplied `args_template` (see "Future Agent Runtime integration").
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
- Automatic selection among `eligible_agents_for()`'s `NEEDS_SELECTION` ties — that authority
  belongs to the founder, or a future orchestration loop operating under explicit founder-set
  policy, never this filter itself.
- Evidence-driven ranking/scoring of agents by past performance — `build_agent_outcome_payload`
  establishes the durable evidence vocabulary only; a Capability Matrix that actually reads it
  is future work, gated on there being enough real evidence accumulated to be meaningful.
- A stored, agent-level heartbeat column — `AgentRuntimeView.heartbeat_at` is derived from the
  most recent `last_heartbeat_at` across an agent's own active leases (per-lease heartbeats
  already exist); a dedicated agent-level column would be a second source of truth for
  information the lease table already carries.
- Wiring `bootstrap_known_agents()` into automatic application boot — remains an explicit,
  founder-invoked action; see that function's own module docstring for why.
