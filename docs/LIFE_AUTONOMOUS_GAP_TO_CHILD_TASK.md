# Autonomous Gap → Child-Task Generation

Branch: `claude/mainai-autonomous-gap-child-task` (based on `claude/det-kommer-mer-879lcm` at
`a6174fb10fa5ad6e2daa630bd9183cf1f0abac93`, the squash-merge of PR #77's partial-plan-insertion
primitive, Alembic head `0045`).

Primitives: `record_gap()`, `assess_gap_authority()`, `propose_child_task_spec()`,
`generate_child_task_for_gap()`, `run_gap_generation()` — `backend/app/autonomous_gap/service.py`.
Tests: `backend/tests/backend/mainai/test_autonomous_gap_child_task.py`.

## Why this exists

The partial-plan-insertion primitive (`insert_plan_tasks()`, `docs/LIFE_PARTIAL_PLAN_INSERTION.md`)
answers "how does a new bounded task get safely added to an existing plan." It deliberately does
NOT answer "should this gap become a task in the first place" — that decision was explicitly out
of scope for that primitive ("This is a MainAI planning primitive. It is NOT yet gap discovery.").

This module is that next layer: while executing an already-authorized goal, MainAI can encounter
a REAL gap — a verification failure, a missing capability, an absent prerequisite — and this
module lets it turn that gap into a bounded child task **without deciding for itself that the gap
deserves work**. Authority is assessed against the same authorized envelope
(`SupervisorScope`) the Scoped Development Supervisor already operates under, and materialization
always flows through the already-reviewed `insert_plan_tasks()` primitive — this module never
writes a `mainai_tasks` row directly.

## Gap sources (what counts as a real gap)

`DiscoveredGap.evidence_kind` must be one of `AUTHORIZED_EVIDENCE_KINDS`:

- `verification_failure` — a real `verification_failed` `MainAITaskEvent` already exists for the
  source task (migration 0032's existing evidence shape, `verify.py`'s `VerificationResult`).
- `capability_missing` — the Development Operator's own `OperatorCapabilityMissing`/
  `capability_missing()` result already recorded the gap as a `WorkTraceEvent`
  (`app/development_operator/service.py`) or Safe Planner's `record_capability_gap()` already
  recorded it as a `MainAICheckpoint`.
- `missing_prerequisite` — deterministic repository inspection proved a prerequisite is absent
  before an existing task can proceed.
- `unresolved_dependency` — an existing, durable dependency/blocker record
  (`LifeIntentBlocker`, an existing `MainAITaskDependency` on a task that will never complete)
  shows required upstream work is missing.
- `repository_inspection` — a deterministic, reproducible check against the actual repository
  state (not an opinion) found the gap.
- `provider_identified_step_validated` — a provider MAY surface a candidate step, but only
  becomes usable evidence after the CALLER independently, deterministically validated it (the
  suggested capability really is missing, the suggested prerequisite really is absent). This
  module has no way to perform that validation itself — the same "structurally required, not
  independently re-verified" boundary `insert_plan_tasks()` already establishes for
  `authority_kind`.

Anything else — `idea`, `suggestion`, `hypothesis`, `ai_interpretation`, or any unrecognized
string — is refused before anything is even recorded (`GapEvidenceError`, nothing written). This
is the "PROVIDER IDEA != AUTHORITY" boundary: a provider's own prose can never itself become
executable work just because it sounded useful.

## Evidence and provenance (reusing existing structures)

`record_gap()` calls `app.problem_learning.service.create_problem()` (migration 0042's
`life_problems` table) rather than inventing a parallel evidence table. This directly satisfies
the founder's GAP RECORD requirement — every field it lists is captured without a new migration:

| Requirement | Where it lives |
|---|---|
| Parent goal | `DiscoveredGap.parent_goal_id`, `provenance["parent_goal_id"]` implicitly via `goal` param |
| Source evidence | `mainai_task_id`/`mainai_job_id` FKs, `provenance["evidence_kind"]` |
| Exact reason | `LifeProblem.description` |
| Gap type | `provenance["gap_type"]` |
| Affected task/job | `LifeProblem.mainai_task_id`/`mainai_job_id` |
| Repository/path scope | `provenance["repository_identity"]`/`["allowed_paths"]` |
| Required outcome | `provenance["required_outcome"]` |
| Authority basis | `LifeProblem.authority` (`"deterministic_source"`) |
| Unknowns/assumptions | `provenance["unknowns"]` |
| Provenance | `provenance["source_type"]`/`["source_ref"]`/`["requested_by"]` |
| Whether unrelated work can continue | `provenance["unrelated_work_can_continue"]` |

`create_problem()` is already idempotent by `idempotency_key` (a UNIQUE constraint,
`uq_life_problems_idem`) and already race-safe: it takes a `SELECT ... FOR UPDATE` lock on the
owner's own `users` row before its idempotency check, which serializes concurrent
`create_problem()` calls for the same owner — the second caller blocks until the first commits,
then finds the already-created row. `record_gap()`'s idempotency key is a deterministic SHA-256
of the gap's semantic identity (`gap_type`, `parent_goal_id`, `source_task_id`/`source_job_id`,
`reason`, `required_outcome`, `existing_task_dependency_target`) — deliberately excluding
incidental proposal detail (risk level, verification plan, capability list), the same exclusion
principle `insert_plan_tasks()`'s own `_semantic_hash()` applies.

## Authority assessment

`assess_gap_authority()` replicates the exact deterministic decision tree
`app.development_supervisor.service.validate_scope()` already establishes for "is this scope's
authority still valid," plus gap-specific checks. Automatic child-task authorization requires
ALL of, checked in order:

1. `scope.authority_kind` is one of `AUTHORIZED_KINDS` (not `NON_AUTHORITATIVE_KINDS`, not
   unrecognized) → else `NEEDS_AUTHORIZATION`.
2. `sha256(goal.original_instruction)` still matches `scope.authorized_instruction_sha256`,
   re-checked live against the goal row at call time → else `NEEDS_AUTHORIZATION` (a founder
   correction superseded the direction; a stale authorization can never execute late).
3. `goal.status` is not terminal → else `OUT_OF_SCOPE`.
4. The gap's `repository_identity`/`allowed_paths` are within the scope's → else `OUT_OF_SCOPE`.
5. If `gap.gap_type` is `capability_missing` or `self_improvement` (i.e. the gap concerns
   MainAI's own capabilities/development independence, not the parent goal's ordinary subject
   matter), `scope.self_work` must be `True` → else `NEEDS_AUTHORIZATION`. This is what
   separates "CAPABILITY_MISSING → AUTHORIZED CHILD" from "CAPABILITY_MISSING →
   NEEDS_AUTHORIZATION" (the identical gap, under scopes that differ only in `self_work`).
6. `gap.required_capabilities` is a subset of `scope.allowed_capabilities` → else
   `CAPABILITY_MISSING`.
7. `gap.risk_level` is within `scope.maximum_risk`'s envelope → else `NEEDS_AUTHORIZATION`.
8. `gap.risk_level` is never `high`, **regardless of the envelope** → else
   `EXTERNAL_REVIEW_REQUIRED` (a self-authorizing loop generating high-risk changes always needs
   a human, even when the envelope technically permits it).
9. `gap.unknowns` is empty → else `NEEDS_CLARIFICATION`.
10. `gap.candidate_options` has at most one entry → else `NEEDS_SELECTION` (this module never
    silently picks a remediation among several equally-plausible ones).
11. `gap.required_outcome` is non-empty → else `NEEDS_CLARIFICATION` (no completion criteria, no
    automatic authorization).

Returning `None` means authorized; the six terminal-state strings above are exactly the
vocabulary the founder's spec requires. This function never guesses — every branch is a concrete,
deterministic comparison against `scope`/`goal`/`gap`, none of it inferred from provider output.

## Child-task proposal and hand-off to partial-plan-insertion

`propose_child_task_spec()` builds an `InsertedTaskSpec` from an already-authorized `gap` — it
makes no authority decision of its own (that already happened). `generate_child_task_for_gap()`
then calls `insert_plan_tasks()` exactly once per gap, inheriting **for free**: atomic
validation, DAG-preserving cycle detection, idempotency/concurrency safety, and approval-policy
enforcement. This module never constructs a `MainAITask`/`MainAITaskDependency` row itself — the
only mutation path into those tables is that one call.

For `missing_prerequisite` gaps, `DiscoveredGap.existing_task_dependency_target` becomes an
`ExistingTaskDependencyEdge` — the new task is inserted and the existing target task gains an
**additive** dependency on it, exactly as `insert_plan_tasks()`'s own docs describe (the
existing task's original dependencies are never rewritten). For every other gap type, the
proposed child depends on nothing by default (an independent new task) — a repair child never
depends on the FAILED task it is fixing, since `insert_plan_tasks()` itself refuses a dependency
on a task that can never complete (`failed`/`cancelled`).

## Idempotency and concurrency (composed, not reimplemented)

Two independently idempotent primitives are composed rather than one being reimplemented on top
of the other:

1. `record_gap()` is idempotent via `create_problem()`'s `idempotency_key`.
2. `insert_plan_tasks()` is called with a SECOND, derived idempotency key
   (`f"autonomous_gap_child:{problem.idempotency_key}"`) — itself idempotent via the
   transaction-scoped advisory lock `insert_plan_tasks()` already establishes.

No additional bookkeeping is needed for either "the same gap discovered twice" or "resume after
an interruption": replaying the whole `record_gap → assess_gap_authority → insert_plan_tasks`
pipeline for the exact same `DiscoveredGap` converges on the same `LifeProblem` row and the same
canonical inserted `MainAITask` row(s) every time, proven by a real two-thread, two-session race
test (`test_security_concurrent_gap_generation_for_the_same_gap_produces_exactly_one_canonical_child`)
and a simulated-interruption test.

## Running/waiting siblings and approval semantics

`generate_child_task_for_gap()` performs no special handling of running/waiting siblings beyond
what `insert_plan_tasks()` already enforces (see `docs/LIFE_PARTIAL_PLAN_INSERTION.md`'s own
"Running and waiting siblings" section) — this module is a caller of that primitive, not a
reimplementation of its safety properties.

Approval is likewise never bypassed: every generated child is a brand-new `MainAITask` id, so
`require_task_approval()` (which looks up `approval_granted` events **by task id**) automatically
requires fresh approval under whichever policy the target goal's `approval_policy` already names.
Neither `source_type` nor `requested_by` play any role in the approval decision, so a
self-work-authorized, AI-labelled gap generation cannot grant itself an execution exemption a
founder-labelled one wouldn't also get. **Gap generation != authorization to execute; insertion
!= approval; approval != execution bypass.**

## Recovery/checkpoint behavior

`generate_child_task_for_gap()` only ever `db.flush()`es inside `record_gap()`/
`insert_plan_tasks()` — it never commits itself, the same caller-owns-the-commit convention every
other primitive in this chain follows. A crash before the caller's own commit rolls back
everything cleanly (proven by the interruption/resume test).

`run_gap_generation()` additionally writes a `MainAICheckpoint` (step
`"autonomous_gap_generation"`, keyed by a caller-supplied `run_id` as the checkpoint's `job_id`)
after each gap that has a real `source_task_id`, recording the gap's index in the run, its
classification, and its `LifeProblem` id. This is durable observability of run progress, **not**
the mechanism actual correctness depends on — correctness under interruption is already
guaranteed by the two composed idempotent primitives above; the checkpoint exists so a caller can
resume a partially-completed run by re-supplying only the unprocessed tail of a gap list, without
needing to re-derive which gaps already landed.

## Bounds

`GapGenerationBounds` — no unbounded recursive expansion:

- `max_gaps_per_run` — stops after this many gaps have been considered in one `run_gap_generation()` call.
- `max_children_per_run` — stops once this many gaps have been `ACCEPTED` (inserted) in one run.
- `max_generation_depth` — `DiscoveredGap.generation_depth` is checked against this bound
  **before** authority assessment even runs; a gap discovered while executing a
  previously-generated child carries `generation_depth = parent.generation_depth + 1`. This
  module never recurses into itself automatically — a caller who discovers a further gap while
  executing a generated child constructs a new `DiscoveredGap` with the incremented depth and
  calls back in, same as any other gap. Reaching the bound returns `"DEPTH_BOUND_REACHED"`
  (evidence still recorded, no insertion) rather than silently continuing.
- `max_elapsed_seconds` — wall-clock bound on one `run_gap_generation()` call.
- `max_unresolved_gaps` — stops if too many gaps in one run land on a non-`ACCEPTED`
  classification (a signal the run is mostly hitting authority walls, not making progress).

A non-`ACCEPTED` outcome for one gap (waiting on clarification, capability-missing, out of
scope, ...) never stops processing of the next gap in the list — "preserve gap A as waiting,
continue gap B" is the default loop behavior, not a special case requiring extra code.

## Self-work

An authorized self-improvement goal (e.g. "Improve Life's deterministic development
independence") can generate a bounded child through the identical chain — `record_gap` →
`assess_gap_authority` → `insert_plan_tasks` → Scoped Development Supervisor → Safe Planner →
Work Driver → Development Operator → verify — with no separate "self-work bypass" code path.
The ONLY thing that differs for a self-work gap is check #5 in "Authority assessment" above
(`scope.self_work` must be `True`); every other check, and the approval gate downstream, applies
identically. There is no vague "improve myself" task generation — `gap.required_outcome` must
still be a concrete, non-empty completion criterion (check #11), same as any other gap.

## Supervisor compatibility

`discover_candidates()` (`app.development_supervisor.service`) queries `mainai_tasks` by
`(owner_id, goal_id)` with no dependency on which code path created a given row — a
gap-generated child is visible to it as ordinary MainAI child work the moment it is committed,
proven directly by test. **This module does not make the Supervisor generate gap-discovery
requests itself** — the Supervisor still only ever executes work that already exists as a
canonical task; nothing in this branch wires the Supervisor's own loop to call into
`generate_child_task_for_gap()`.

## What this is explicitly NOT / what remains deferred

This branch builds and tests the gap → child-task **primitive** — the same scope boundary
`insert_plan_tasks()` itself was accepted at (a callable, thoroughly tested module, not yet wired
into any live autonomous loop). Concretely NOT implemented here:

- **Autonomous continuation is not wired into the executor/driver/operator control flow.**
  `execution_job.py`'s verification-failure finalize path, `development_operator.py`'s
  `capability_missing()`, and `development_driver.py`'s `RESULTS` handling do not automatically
  construct a `DiscoveredGap` and call this module today. The primitive exists and is proven
  correct against real evidence rows constructed the same way those call sites already produce
  them; wiring the live loop to invoke it automatically is a further integration step, not
  attempted in this branch, for the same reason PR #77 didn't wire itself into any caller either
  — it changes the blast radius from "one new, isolated module" to "every existing autonomous
  execution path," which deserves its own dedicated, isolated review.
- Autonomous top-level goal creation (`generate_child_task_for_gap()` never constructs a
  `MainAIGoal` — proven by test).
- Global cross-goal prioritization.
- Unrestricted recursive planning or world-model planning.
- Unrestricted self-modification or autonomous capability installation outside the
  goal/plan/approval flow.
- Automatic PR creation, merge, deploy, or any production mutation.

## Remaining architectural gap before Life can drive broader self-development

Wiring this primitive into the live execution loop (the bullet above) is the concrete next step.
Once that exists, the remaining gap is a genuine **prioritization** layer: when multiple
authorized gaps compete for bounded execution capacity across many goals simultaneously, something
above both this module and the Work Driver needs to decide global ordering — explicitly deferred
here (`GapGenerationBounds` only bounds a single `run_gap_generation()` call over an
already-goal-scoped gap list; it does not compare gaps across different goals).
