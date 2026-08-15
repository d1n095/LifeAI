# LIFE AUTONOMOUS DEVELOPMENT LOOP / WORK DRIVER

The Work Driver is a deterministic control layer above existing MainAI orchestration. It is
not a scheduler, queue, coding agent, repository abstraction, or recursive model loop.
MainAI goals, plans, tasks, jobs, leases, approvals, checkpoints, waits, recovery and
verification remain canonical execution truth. Repository effects always go through the
Life Development Operator.

## Control flow and reuse

An already-scoped goal is assessed against existing Life Intent feasibility. Required
dependencies and active blockers prevent execution; unrelated actionable work remains
selectable. Equal-priority ambiguity produces `NEEDS_SELECTION` rather than invented intent.
The driver creates or refreshes an existing bounded Active Context set anchored to the
MainAI task. Context membership contains typed references and explainable relationship paths,
never copied messages, documents, evidence, or source truth. Memory Threads remain the
durable-history layer used by Active Context.

The canonical MainAI plan/task remains the work breakdown. A driver invocation receives a
bounded structured capability plan attributed to an existing 0043 Work Strategy Execution.
Each step states its purpose, expected result, capability, structured arguments, declared
risk, evidence expectation, verification implication, and failure behavior. Validation
checks owner/task/job/execution linkage, live fenced lease, worktree/base/branch identity,
approval policy, capability vocabulary, risk, action bounds and forbidden command-shaped
arguments. Unknown capabilities become explicit capability gaps; shell, merge, deploy,
production mutation and force-push fail closed.

The lifecycle is explainable through `ASSESS`, `PLAN`, `EXECUTE`, `VERIFY`, `REVIEW`,
`LEARN`, `RE_EVALUATE`, `WAIT`, `BLOCKED`, `COMPLETE`, `FAILED`, and `CANCELLED` concepts.
These do not add another database state machine: current driver phase/result and structured
resume state are append-only MainAI checkpoints. Every invocation has action, elapsed-time,
failure, lease and cancellation bounds. It executes one governed operator action at a time,
records the canonical 0043 trace, checkpoints, classifies the result, and either proceeds or
stops with an exact reason.

Operator idempotency plus driver plan hashes prevent completed writes from being silently
repeated after restart. Conflicting replay fails closed. The existing V0.2 worktree marker,
job lease generation, recovery inspection and takeover mechanisms remain authoritative;
the driver creates no alternative recovery path.

## Verification, waits and learning

Action success is not task success. Required operator test/static/migration observations are
collected first and a separate MainAI verification checkpoint records whether all required
evidence passed. Missing or failed verification prevents completion. Successful completion
uses MainAI's canonical task finalization and fenced job completion paths. Approval remains
enforced before any driver action.

A specialist-review directive checkpoints the exact unresolved question and bounded context.
A provider-dependent step becomes `WAITING_PROVIDER`; completed local work and the isolated
workspace remain durable, and the result states whether independent work remains. Provider
state never owns canonical progress. A missing operator capability creates both an audited
0043 action and a driver checkpoint linked to the goal/task/job, explaining why it is needed
and whether other work can continue. It never installs or implements the capability itself.

Real actions feed existing 0042 problem/approach/decision/outcome learning, 0043 strategy,
trace, navigation, efficiency, verification, waste and stopping evidence, 0044 comparison /
experiment / promotion proposals, and 0045 synthesis provenance. The driver does not create
parallel learning truth. LifeAI self-work receives exactly the same branch, worktree,
capability, verification, approval, commit and optional push gates as any other repository.

Continue only while authority, lease, workspace, dependencies, capability, risk policy,
approval, deterministic next step and budgets remain valid. Stop/checkpoint on completion,
failed or missing verification, capability gaps, approval/review/provider waits, bounds,
cancellation, lease loss, unexpected repository state, security violation or ambiguity.

## Explicitly deferred

Global autonomous prioritization, provider/quota routing, model leaderboards, specialist-team
construction, automatic strategy activation/promotion, capability installation, universal or
multimodal ingestion, world-model planning, autonomous PR creation, merge, deployment,
production mutation, unrestricted shell/browser control, reinforcement-style policy changes
and unrestricted Life Core self-modification remain outside this foundation.
