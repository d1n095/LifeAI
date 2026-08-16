# Partial Plan Insertion

Branch: `claude/mainai-partial-plan-insertion-foundation` (based on
`claude/det-kommer-mer-879lcm` at `91b9908c29964c55e6a129ea07c027460798f010`, the head of the
fully-merged, reconciled 12-layer autonomy chain + P1 approval fix, Alembic head `0045`).

Primitive: `insert_plan_tasks()` — `backend/app/mainai_execution/plan_insertion.py`.
Tests: `backend/tests/backend/mainai/test_partial_plan_insertion.py`.

## Why this exists

The reconciled autonomy chain (Layers #1-#13, PRs #62-#75) gives MainAI a durable Goal → Plan →
Task execution loop, but only ever gets a full task breakdown one way: `planner.create_plan()`.
Calling `create_plan()` again for a goal that already has an active plan is a **full replan** —
it supersedes the previous plan wholesale, cancelling every still-non-terminal task. That is the
correct tool when the founder (or `app/mainai_execution/replan.py`'s automatic replan trigger)
genuinely wants a fresh breakdown, but it is the *wrong* tool for the much more common case: the
plan is fundamentally still right, and exactly one or two additional bounded tasks need to be
added to it — a missed edge case, a follow-up fix, a newly-authorized piece of work that belongs
next to what is already running.

Before this primitive, there was no way to do that without either (a) cancelling and recreating
everything via `create_plan()` — destroying in-flight state, dependency history, and verification
progress for no reason — or (b) writing directly to `mainai_tasks`/`mainai_task_dependencies`
from some other call site, bypassing every validation, authority, and audit guarantee the planner
already establishes. `insert_plan_tasks()` closes that gap: it is the one sanctioned way to grow
an existing, active plan safely.

This is explicitly a **prerequisite for, not the same as,** the future "Autonomous Gap →
Child-Task Generation" feature. That feature will decide *whether* a newly-noticed problem
deserves a task. This primitive only decides *how* a task — once some upstream caller has already
supplied valid authority for it — gets safely written into an existing plan without breaking
anything already there. See "What this is explicitly NOT" below.

## Full-plan supersession vs. partial insertion

|                                   | `create_plan()` (replan)                          | `insert_plan_tasks()` (this primitive)         |
|-----------------------------------|----------------------------------------------------|--------------------------------------------------|
| Creates a new `MainAIPlan` row    | Yes — new version, old plan `superseded`           | No — inserts into the SAME active plan            |
| Existing non-terminal tasks       | Cancelled                                          | Left completely untouched                         |
| Existing task ids                 | Not reused (new plan, new tasks)                   | Never change                                       |
| Existing dependency edges         | Discarded with the old plan                        | Never rewritten or removed — only ADDED to         |
| Verification/checkpoint state     | Reset for cancelled tasks                          | Untouched for every existing task                  |
| Intended trigger                  | Founder wants a fresh breakdown, or a genuine replan condition | A bounded addition to an otherwise-correct plan |

`insert_plan_tasks()` never calls `create_plan()` and never constructs a `MainAIPlan` row. It
only ever `INSERT`s new `mainai_tasks` rows, `mainai_task_dependencies` rows for the new tasks'
own edges, and — for an existing task gaining a new prerequisite — exactly one additional
`mainai_task_dependencies` row pointing at it. No existing row is ever `UPDATE`d except an
existing task's own dependency set gaining one new edge (see "Dependency graph" below); no
existing row is ever cancelled, superseded, or recreated.

## Authority boundary

`insert_plan_tasks()` does not decide whether a piece of work deserves to exist. It requires the
caller to supply:

- `authority_kind`, which must be one of `AUTHORIZED_KINDS` — `founder_requirement`,
  `founder_decision`, `founder_correction`, `authorized_goal`. Anything in
  `NON_AUTHORITATIVE_KINDS` (`founder_preference`, `idea`, `suggestion`, `hypothesis`,
  `ai_interpretation`, `unknown`) or any unrecognized string is rejected before any other
  validation runs — fail closed, no partial work.
- `authorized_instruction_sha256`, the SHA-256 of `goal.original_instruction` **at the moment the
  caller decided to authorize this insertion**. The function re-reads `goal.original_instruction`
  fresh, inside its own lock, and re-hashes it — if a founder correction changed the instruction
  between authorization and execution, the hashes no longer match and the insertion is rejected
  (`PlanInsertionAuthorityError`). A stale authorization can never execute late.

This is deliberately the same vocabulary `app/safe_planner/service.py`'s `AUTHORIZED_KINDS`/
`NON_AUTHORITATIVE_KINDS` already establish, for the identical reason: a provider's own output,
an idea, or a hypothesis can never itself grant execution authority. The two frozensets are
duplicated locally (not imported) — `app.safe_planner.service` already imports from
`app.mainai_execution` (both `approval` and `checkpoint`), so importing the reverse direction
would create a circular import. Both copies must be kept in sync by hand if the vocabulary ever
changes; this file's own module docstring says the same.

The function has no way to verify a caller's claimed `authority_kind`/`authorized_instruction_sha256`
are honest — verifying *that* is the caller's own responsibility, exactly as it already is one
layer up in `app.safe_planner.service.assess_authority()`. What this function structurally
guarantees is that it **refuses to proceed** on anything it can recognize as insufficient or
stale, rather than trusting the caller's label.

## Atomic validation

Every task spec and dependency edge in one call is validated **before any row is written**. The
full validation pass runs first — description, `task_type`, `verification_plan` entries,
`depends_on` indices/uuids, existing-task-dependency edges, and a full cycle check over the
combined existing+new dependency graph — and only after every check in the batch passes does the
function persist anything. If any one spec or edge in a multi-task insertion is invalid, **nothing
is inserted** — not the earlier, valid specs either. This mirrors `create_plan()`'s own
all-or-nothing validate-then-persist structure.

Validated, at minimum: owner consistency (plan/goal owner match); goal exists and plan belongs to
it; plan is `active` (not `draft`/`superseded`); authority kind and instruction hash (see above);
task type is known (`KNOWN_TASK_TYPES`); `verification_plan` entries are well-formed and any
`targeted_tests` target is path-safe (`validate_targeted_tests_target()`); dependency indices are
in range and not self-referential; `depends_on` uuids resolve to an existing task *in this same
plan* (see "Cross-owner isolation" below); no dependency on a task in a status that can never
complete (`failed`/`cancelled`); `existing_task_dependencies` edges only target a task in a status
that can safely receive one (see "Running/waiting siblings" below); and no cycle exists in the
combined graph of existing-plan edges + this insertion's new edges.

## DAG preservation

Cycle detection reuses `planner._detect_cycle()` — the identical three-color DFS the planner
already uses at plan-creation time — over a **combined node space**: every existing task in the
plan keeps its real `MainAITask.id` as a graph node, and every new spec in this insertion is
addressed by its own 0-based index, offset so the two node spaces never collide. The edge set fed
into it is: every existing `mainai_task_dependencies` row for a task in this plan, every
`depends_on` edge inside the new specs (new→new or new→existing), and every
`existing_task_dependencies` edge (existing→new). If that combined graph contains a cycle, the
whole insertion is rejected — nothing is written, existing edges are never touched.

Inserting a prerequisite between two already-linked existing tasks (existing A → B, insert C such
that A → C → B) is **purely additive**: B's original edge to A is left completely untouched, and
a new edge B → C is added, so B ends up depending on both A and C. This function never rewrites
or removes an edge that already existed — a true edge *replacement* would be a different,
more destructive primitive this feature deliberately does not provide.

## Idempotency and concurrency

No new table and no new migration were needed — `mainai_tasks`, `mainai_task_dependencies`, and
`mainai_task_events` (all from migration 0032) are already exactly the schema an insertion needs.
Genuine, race-safe idempotency is achieved without a new `UNIQUE` column:

1. A **transaction-scoped Postgres advisory lock**, `pg_advisory_xact_lock(hashtextextended('mainai_plan_insertion:{plan_id}', 2))`
   — the same `pg_advisory_xact_lock(hashtextextended(...))` idiom `app/storage/references.py`'s
   `acquire_storage_key_lock()`/`acquire_owner_erasure_lock()` already use (seeds 0 and 1
   respectively; this primitive uses seed 2). It serializes every insertion attempt against the
   *same plan* and releases automatically at the caller's next commit or rollback.
2. Inside that lock, the caller's `idempotency_key` and a canonical SHA-256 **semantic hash** of
   the whole insertion request (task specs + existing-task-dependency edges + authority kind +
   provenance — deliberately excluding incidental metadata like `reason`/`requested_by`/
   timestamps, the same exclusion principle `app/development_driver/service.py`'s `_plan_hash()`
   and `app/safe_planner/service.py`'s `_candidate_hash()` already apply) are recorded inside the
   `created` `MainAITaskEvent`'s own `detail` JSON for each newly inserted task.
3. A replay lookup, performed **inside the same lock**, searches for an existing `created` event
   in this plan carrying that exact `idempotency_key` (`detail ->> 'insertion_idempotency_key'`,
   via SQLAlchemy's `.op("->>")` — see the implementation note below). If found and the recorded
   semantic hash matches, the function returns the **same canonical task rows** a prior identical
   call already created — no new rows, no duplicate dependency edges. If found with a *different*
   semantic hash, the call fails closed (`PlanInsertionConflictError`) rather than guessing which
   version was "right."

Because the lock is transaction-scoped and Postgres runs at `READ COMMITTED` by default, two
concurrent callers racing the same `(plan_id, idempotency_key)` are genuinely serialized: the
second caller blocks on the advisory lock until the first caller's transaction ends, and — since
each statement takes a fresh snapshot under `READ COMMITTED` — sees the first caller's committed
rows on its own replay lookup. This is proven by a real two-thread, two-session test (`test
8: Concurrent Insert`), not simulated with manual signalling.

**Implementation note**: `MainAITaskEvent.detail` is mapped with SQLAlchemy's generic `JSON` type
in `app/models/mainai_execution.py` (not `sqlalchemy.dialects.postgresql.JSONB`), even though the
underlying Postgres column genuinely is `jsonb` (migration 0032). The JSONB-only `.astext`
comparator is therefore unavailable on this column; the replay lookup uses
`MainAITaskEvent.detail.op("->>")("insertion_idempotency_key")` instead, which emits the identical
Postgres `->>` operator directly regardless of which Python-side type the column is mapped with.

## Running and waiting siblings

A **new** task may depend on an existing task in *any* status except one that can provably never
complete (`failed`/`cancelled`, `_NEVER_COMPLETES_STATUSES`) — that is what makes "insert
follow-up work after X, whatever X's current state" possible at all. Depending on a task that is
`waiting_external`/`waiting_ci`/`running`/already `completed` is all fine; a new task can wait for
any of them.

The **reverse** direction — an *existing* task gaining a brand-new dependency on a task this same
insertion is creating — is far more restrictive, by design: it is only accepted when the existing
task's live status (re-read under a fresh `SELECT ... FOR UPDATE` row lock, not the caller's
possibly-stale in-memory copy — closing a race against a concurrent `recompute_task_readiness()`/
`dispatch_ready_task()` call that does not participate in this primitive's plan-level advisory
lock) is `pending` or `blocked`. A task already `ready` might be claimed by a dispatcher at any
moment; a `running` task is already executing; a `waiting_*` task is already in a durable external
wait; a terminal task is already decided. Retroactively adding a prerequisite to any of those is
refused, fail-closed (`PlanInsertionValidationError`), never silently ignored or forced through.
Insertion must never corrupt active execution — a task that is genuinely safe to interrupt has an
existing, reviewed mechanism for that (V0.2's dead-agent recovery / takeover), which this
primitive does not touch or duplicate.

An unrelated insertion — one that does not target an existing task via
`existing_task_dependencies` — always succeeds regardless of any sibling's status; only the
specific *retroactive-prerequisite* operation is status-gated.

## Task order / priority

No opaque AI-generated score is introduced. `InsertedTaskSpec.priority` is the same integer field
`PlannedTaskSpec.priority` already is, consumed by the exact same ordering
`graph.next_ready_task()` already applies (`priority DESC, created_at ASC` — FIFO within a
priority tier). If the caller supplies an explicit priority, it is preserved as given. No existing
sibling's priority, position, or ordering is ever touched by an insertion; only a new,
explicitly-authorized `existing_task_dependencies` edge can change *when* an existing task becomes
eligible to run, and only in the additive way "DAG preservation" above describes.

## Cross-owner isolation

Every `depends_on` uuid reference and every `existing_task_dependencies.existing_task_id` is
resolved against a lookup built **strictly from tasks belonging to the target plan**
(`SELECT ... WHERE plan_id = :plan_id`). A uuid belonging to another plan — whether that plan
belongs to the same owner or a different one — simply is not present in that lookup and is
rejected as "not an existing task in this plan (unknown, cross-owner, or belongs to a different
plan)," the identical `PlanInsertionValidationError` a genuinely unknown uuid would produce. This
makes cross-owner isolation a structural property of the query, independent of Row-Level Security
— and RLS itself is still exercised end-to-end by this primitive's own owner-scoped tests.

## Approval semantics

Insertion and approval are two entirely separate concerns. `insert_plan_tasks()` never creates,
grants, or references an `approval_granted` `MainAITaskEvent`. Every newly inserted task is a
brand-new `MainAITask` row with its own id, so `app/mainai_execution/approval.py`'s
`require_task_approval()` — which looks up `approval_granted` events **by the task's own id** —
automatically requires fresh approval for it under whichever policy the target goal's
`approval_policy` already names (`standard_repo_work` or `autonomous_development_work`, the P1
fix from the prior merged chain). No special-casing was needed, and none was added: **insertion
!= approval, insertion != execution.** An inserted task cannot be dispatched
(`executor.dispatch_ready_task()`) until it clears the exact same approval gate every other task
does.

`source_type`/`requested_by` (who or what proposed the insertion) play no role in the approval
decision either — the gate only ever looks at `task_type` + the goal's named policy, so an
AI-labelled source cannot grant itself an exemption a founder-labelled source wouldn't also get
(see the "no self-work bypass" security test).

## Recovery and checkpoint behavior

No second audit system was created. Insertion durably records, inside the **existing**
`mainai_task_events` table, using the **existing** `created` event type for each new task and the
**existing** `progress_updated` event type for an existing task that gained a new prerequisite
(extending migration 0032's `mainai_task_events_event_type` CHECK constraint for a purely
informational annotation would need a new migration this feature does not require). Each event's
`detail` carries: `plan_version`, `insertion_idempotency_key`, `insertion_semantic_hash`,
`insertion_task_id`, `insertion_task_index`, `authority_kind`, `source_type`, `source_ref`,
`reason`, `requested_by`, and a `kind: "partial_plan_insertion"` tag.

`insert_plan_tasks()` only ever `db.flush()`es — it never calls `db.commit()` itself, the same
caller-owns-the-commit convention `planner.create_plan()` and `executor.dispatch_ready_task()`
already follow. This gives "no partial state on interruption" for free: a crash before the
caller's own commit rolls back everything this function wrote, including releasing the
transaction-scoped advisory lock automatically (`test 15: Interruption/Resume` proves this against
a real Postgres rollback, then proves a retried call with the same idempotency key succeeds
cleanly as a fresh insertion, since nothing from the interrupted attempt actually landed).

V0.2 (dead-agent recovery/takeover) and V0.3 (wait/replan/resume) continue to work unmodified
after an insertion: neither of them cares how a task came to exist, only about its current,
durable row and event history, both of which this primitive produces in exactly the same shape
`create_plan()` already does.

## Supervisor compatibility

The Scoped Development Supervisor's own candidate discovery
(`app/development_supervisor/service.py`'s `discover_candidates()`) queries `mainai_tasks` by
`(owner_id, goal_id)`, ordered by priority/creation time — it has no dependency on which code path
created a given task. A task inserted by `insert_plan_tasks()` is visible to it as ordinary MainAI
child work the moment it is committed, with no special wiring required (`test 16: Supervisor
Compatibility` proves this).

**This feature does NOT make the Supervisor generate insertion requests.** It proves visibility
only — the Supervisor still only ever *executes* work that already exists as a canonical task; it
has no code path in this branch that calls `insert_plan_tasks()` itself.

## What this is explicitly NOT

This is a MainAI **planning primitive** — how work already deemed appropriate can be safely added
to an existing plan. It is deliberately **not**:

- Gap discovery. Nothing in this branch looks at execution failures, verification results, or
  provider output and decides that new work is needed.
- Autonomous authority inference. `insert_plan_tasks()` never grants itself authority; it only
  refuses to proceed without an already-valid `authority_kind` supplied by the caller.
- A second planner, task system, or goal system. It is one new function in
  `app.mainai_execution`, reusing every existing model, event type, approval policy, and
  readiness/cycle-detection helper the planner already established.
- Autonomous replanning, automatic PR creation, merge, deploy, or any production-affecting
  action. It writes exactly three kinds of row — `mainai_tasks`, `mainai_task_dependencies`,
  `mainai_task_events` — inside the existing local test/dev database this branch was verified
  against, and touches nothing else.

The remaining architectural gap before "Autonomous Gap → Child-Task Generation" can be built is a
component that decides *when* a gap is real and *what* authority covers it — this primitive
supplies the safe insertion mechanism that component will call into, once it exists.
