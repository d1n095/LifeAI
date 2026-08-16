"""PARTIAL PLAN INSERTION -- the architectural primitive this module exists to add: safely
insert one or more new bounded child tasks into an EXISTING, authorized, active plan without
cancelling, superseding, or otherwise mutating any valid existing sibling task.

Genuinely different from planner.py's create_plan() replan path, on purpose:

  - create_plan() called again for a goal that already has an active plan SUPERSEDES it --
    cancels every still-non-terminal task and starts a brand-new plan version. That is the
    correct tool when the founder (or an automatic replan trigger, app/mainai_execution/
    replan.py) wants a full fresh breakdown.
  - insert_plan_tasks() (this module) is the opposite: it ADDS to the CURRENT active plan.
    Existing task rows keep their ids, their status, their event history, their dependency
    edges, their checkpoints. Nothing already there is ever cancelled, recreated, or rewritten
    by this function -- it only ever INSERTs new mainai_tasks/mainai_task_dependencies rows
    and (for an existing task that gains a new prerequisite) one additional
    mainai_task_dependencies row pointing at it.

This is a MainAI PLANNING primitive, not gap discovery. It does not decide whether a
newly-noticed problem deserves a task -- it only inserts work after an upstream caller
(a founder action, or a future Autonomous Gap -> Child-Task Generation feature this module
explicitly does NOT implement) already supplies valid authority. See AUTHORIZED_KINDS/
NON_AUTHORITATIVE_KINDS below -- deliberately the same vocabulary app/safe_planner/
service.py's AUTHORIZED_KINDS/NON_AUTHORITATIVE_KINDS already establishes for the identical
reason (a provider's own output, an idea, a hypothesis can never itself grant authority to
execute) -- NOT imported from that module, since app/safe_planner depends on this package
(app/mainai_execution), and a reverse import would be a circular, backwards layering
dependency. Keep both lists in sync by hand if the vocabulary ever changes.

No new tables, no new migration: `mainai_tasks`/`mainai_task_dependencies`/
`mainai_task_events` (migration 0032) are already exactly the schema an insertion needs.
Idempotency/concurrency-safety is real (a genuine Postgres UNIQUE constraint is how every
OTHER MainAI-family table achieves it -- see e.g. migration 0026's
`uq_mainai_jobs_owner_idempotency_key`) but achieved WITHOUT a new column here: a
transaction-scoped Postgres advisory lock scoped to `(owner_id, plan_id)` (same
`pg_advisory_xact_lock(pg_catalog.hashtextextended(key, N))` idiom app/storage/references.py's
`acquire_storage_key_lock`/`acquire_owner_erasure_lock` already use, seed 2 -- seeds 0 and 1
are already taken by those two) serializes every insertion attempt against the SAME plan, and
the idempotency key + a canonical semantic hash of the whole atomic insertion are recorded
inside the `created` MainAITaskEvent's own `detail` JSON for each inserted task -- looked up,
inside the lock, by a plain `detail->>'insertion_idempotency_key'` match. This is real,
race-safe idempotency (see this module's own tests for concurrent-caller proof), not merely
"prefer existing schema for its own sake" -- see this file's own docstring section further
down, and docs/LIFE_PARTIAL_PLAN_INSERTION.md, for why this was judged sufficient and migration
0046 was NOT required."""

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.planner import KNOWN_TASK_TYPES, _detect_cycle
from app.mainai_execution.verify import VerificationStepError, validate_targeted_tests_target
from app.models.mainai_execution import (
    TERMINAL_MAINAI_TASK_STATUSES,
    MainAIGoal,
    MainAIGoalRiskLevel,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)

# Mirrors app/safe_planner/service.py's identically-named frozensets -- see this module's own
# docstring for why they are duplicated here rather than imported.
AUTHORIZED_KINDS = frozenset(
    {"founder_requirement", "founder_decision", "founder_correction", "authorized_goal"}
)
NON_AUTHORITATIVE_KINDS = frozenset(
    {"founder_preference", "idea", "suggestion", "hypothesis", "ai_interpretation", "unknown"}
)

# A new task can never depend on an existing task that will PROVABLY never complete -- the
# identical set app/mainai_execution/graph.py's recompute_task_readiness() already uses to
# decide a dependent must become `blocked`, not silently `pending` forever. Kept as its own
# local definition (not imported -- graph.py's is a private `_`-prefixed module constant) but
# intentionally the same expression, so validation-time and runtime-graph semantics can never
# drift apart.
_NEVER_COMPLETES_STATUSES = TERMINAL_MAINAI_TASK_STATUSES - {MainAITaskStatus.completed}

# An existing task may only gain a brand-new dependency edge if it was never promoted past
# these two statuses -- see insert_plan_tasks()'s own docstring ("RUNNING / WAITING TASKS").
_SAFE_TO_ADD_PREREQUISITE_STATUSES = frozenset({MainAITaskStatus.pending, MainAITaskStatus.blocked})

MAX_NEW_TASKS_PER_INSERTION = 20
MAX_EXISTING_TASK_DEPENDENCIES_PER_INSERTION = 20


class PlanInsertionError(Exception):
    """Base for every error this module raises -- never a bare ValueError/RuntimeError, same
    convention planner.py's PlannerError establishes."""


class PlanInsertionAuthorityError(PlanInsertionError):
    """Fail-closed: `authority_kind` is not one of AUTHORIZED_KINDS, or the goal's live
    `original_instruction` no longer matches the hash the caller supplied -- a founder
    correction landed between when the caller read the instruction and this call. Either way,
    nothing is written."""


class PlanInsertionValidationError(PlanInsertionError):
    """A structurally invalid insertion request -- unknown task_type, out-of-range/dangling/
    cross-owner/self dependency, a cycle, an attempt to add a prerequisite to an existing task
    that is not safely able to receive one, or a plan that is not in a state insertion is
    allowed in. Nothing is written; see insert_plan_tasks()'s own docstring for the "validate
    the COMPLETE proposed insertion before writing anything" guarantee."""


class PlanInsertionConflictError(PlanInsertionError):
    """The supplied `idempotency_key` was already used, for THIS plan, with semantically
    different content -- refuses to silently reinterpret an old key as authorizing new,
    different work. Fails closed rather than guessing which version was "right.\""""


@dataclass(frozen=True)
class InsertedTaskSpec:
    """One new task to insert. `depends_on` entries are resolved independently and may be
    EITHER an `int` (a 0-based index into THIS SAME insertion's own `tasks` list -- identical
    convention to planner.py's PlannedTaskSpec.depends_on) OR a `uuid.UUID` (the real,
    already-persisted id of an EXISTING task in the target plan)."""

    description: str
    task_type: str
    depends_on: tuple[int | uuid.UUID, ...] = field(default_factory=tuple)
    risk_level: MainAIGoalRiskLevel = MainAIGoalRiskLevel.low
    approval_required: bool = False
    verification_plan: tuple[dict, ...] = field(default_factory=tuple)
    priority: int = 0
    max_attempts: int = 3


@dataclass(frozen=True)
class ExistingTaskDependencyEdge:
    """One edge where an EXISTING task in the target plan gains a new dependency on a task
    THIS SAME insertion is creating. `existing_task_id` must currently be `pending` or
    `blocked` (see _SAFE_TO_ADD_PREREQUISITE_STATUSES) -- this is purely ADDITIVE: any
    dependency edge the existing task already had is left completely untouched, so the
    existing task ends up depending on BOTH its original prerequisites AND this new one, never
    a replacement of one for the other (see insert_plan_tasks()'s own docstring, "NO FULL-PLAN
    SUPERSESSION")."""

    existing_task_id: uuid.UUID
    depends_on_new_task_index: int


def _semantic_hash(
    *,
    authority_kind: str,
    tasks: tuple[InsertedTaskSpec, ...],
    existing_task_dependencies: tuple[ExistingTaskDependencyEdge, ...],
    source_type: str,
    source_ref: str,
) -> str:
    """Canonical content hash of an insertion request -- deliberately excludes `reason`/
    `requested_by`/timestamps (incidental request metadata, not semantic content), same
    exclusion principle app/development_driver/service.py's `_plan_hash()` and
    app/safe_planner/service.py's `_candidate_hash()` already apply. Two calls with the same
    `idempotency_key` are the SAME request only if this hash also matches (see
    PlanInsertionConflictError)."""
    payload = {
        "authority_kind": authority_kind,
        "source_type": source_type,
        "source_ref": source_ref,
        "tasks": [
            {
                "description": t.description,
                "task_type": t.task_type,
                "depends_on": [str(d) if isinstance(d, uuid.UUID) else d for d in t.depends_on],
                "risk_level": t.risk_level.value,
                "approval_required": t.approval_required,
                "verification_plan": list(t.verification_plan),
                "priority": t.priority,
                "max_attempts": t.max_attempts,
            }
            for t in tasks
        ],
        "existing_task_dependencies": [
            {"existing_task_id": str(e.existing_task_id), "depends_on_new_task_index": e.depends_on_new_task_index}
            for e in existing_task_dependencies
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _acquire_plan_insertion_lock(db: Session, plan_id: uuid.UUID) -> None:
    """Transaction-scoped advisory lock serializing every insertion attempt against THIS plan
    -- released automatically at this transaction's next commit/rollback, exactly like
    acquire_storage_key_lock()/acquire_owner_erasure_lock() (app/storage/references.py). Seed
    2 -- see this module's own docstring for why 0/1 are already taken."""
    db.execute(
        sa_text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 2))"),
        {"key": f"mainai_plan_insertion:{plan_id}"},
    )


def _find_replay(db: Session, *, plan_id: uuid.UUID, idempotency_key: str) -> list[MainAITaskEvent]:
    """Looks up every `created` MainAITaskEvent, for a task in `plan_id`, whose `detail` JSON
    carries this exact `insertion_idempotency_key` -- the read side of the idempotency
    contract. Must only ever be called while _acquire_plan_insertion_lock() is held, or two
    concurrent callers could both see "no replay" and both insert.

    Uses the raw Postgres `->>` operator via `.op()` rather than `.astext`: `detail` is
    mapped with SQLAlchemy's generic `JSON` type (see app/models/mainai_execution.py), not
    `sqlalchemy.dialects.postgresql.JSONB` -- `.astext` is a JSONB-comparator-only attribute
    and raises AttributeError against a generic JSON column, even though the underlying
    Postgres column type genuinely is `jsonb` (migration 0032). `.op("->>")` emits the exact
    same operator directly, independent of which Python-side type the column happens to be
    mapped with."""
    rows = (
        db.execute(
            select(MainAITaskEvent)
            .join(MainAITask, MainAITaskEvent.task_id == MainAITask.id)
            .where(
                MainAITask.plan_id == plan_id,
                MainAITaskEvent.event_type == MainAITaskEventType.created,
                MainAITaskEvent.detail.op("->>")("insertion_idempotency_key") == idempotency_key,
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def insert_plan_tasks(
    db: Session,
    *,
    goal: MainAIGoal,
    plan: MainAIPlan,
    authority_kind: str,
    authorized_instruction_sha256: str,
    idempotency_key: str,
    tasks: list[InsertedTaskSpec],
    existing_task_dependencies: list[ExistingTaskDependencyEdge] | None = None,
    source_type: str,
    source_ref: str,
    reason: str,
    requested_by: str,
) -> list[MainAITask]:
    """Atomically inserts `tasks` (and any `existing_task_dependencies` edges) into `plan`,
    which must be `goal`'s CURRENT active plan. Returns the canonical, persisted MainAITask
    rows for every spec in `tasks`, in the same order -- either newly created, or (on an exact
    idempotent replay) the SAME rows a prior identical call already created.

    AUTHORITY BOUNDARY: `authority_kind` must be one of AUTHORIZED_KINDS -- an idea/suggestion/
    hypothesis/ai_interpretation/unknown kind, or provider output masquerading as one of the
    authorized kinds, is rejected before anything else is even validated (this function has no
    way to verify a caller's claimed `authority_kind` is honest, but it structurally REFUSES to
    proceed on a non-authoritative or unrecognized one -- the actual honesty check is the
    caller's responsibility, same boundary app/safe_planner/service.py's assess_authority()
    already establishes one layer up). `authorized_instruction_sha256` must match
    `sha256(goal.original_instruction)` AT THE MOMENT this function actually runs (the goal row
    is re-locked fresh, not read from whatever the caller happened to load earlier) -- a
    founder correction that changed the instruction between when the caller decided to insert
    and when this call actually executes invalidates the insertion.

    ATOMIC VALIDATION: every task spec and dependency edge is validated BEFORE any row is
    written -- if any one fails, nothing is inserted (mirrors planner.py's create_plan()).

    NO FULL-PLAN SUPERSESSION: unlike create_plan(), this function never sets any existing
    task's status, never cancels anything, never creates a new MainAIPlan row, and never
    touches an existing MainAITaskDependency row -- it only INSERTs new mainai_tasks/
    mainai_task_dependencies rows. recompute_task_readiness() is called once at the end, which
    is itself side-effect-free for anything whose dependency set didn't just change.

    RUNNING / WAITING TASKS: a NEW task may depend on an existing task in ANY status (that's
    what makes "insert work after X, whatever X's current state" possible), except a status in
    _NEVER_COMPLETES_STATUSES (failed/cancelled -- a dependency that can provably never
    complete is rejected up front, not silently created blocked forever). The REVERSE --an
    EXISTING task gaining a dependency on a NEW task via `existing_task_dependencies`-- is
    accepted only from `pending`/`blocked` (see _SAFE_TO_ADD_PREREQUISITE_STATUSES); a `ready`
    task might be claimed by a dispatcher at any moment, and a `running`/terminal/waiting_*
    task is already executing or already decided -- retroactively adding a prerequisite to any
    of those is refused, fail-closed, never silently ignored or forced through."""
    if not tasks:
        raise PlanInsertionValidationError("An insertion must contain at least one task.")
    if len(tasks) > MAX_NEW_TASKS_PER_INSERTION:
        raise PlanInsertionValidationError(f"An insertion may create at most {MAX_NEW_TASKS_PER_INSERTION} tasks.")
    existing_task_dependencies = list(existing_task_dependencies or [])
    if len(existing_task_dependencies) > MAX_EXISTING_TASK_DEPENDENCIES_PER_INSERTION:
        raise PlanInsertionValidationError(
            f"An insertion may add at most {MAX_EXISTING_TASK_DEPENDENCIES_PER_INSERTION} existing-task dependency edges."
        )
    if not idempotency_key or not idempotency_key.strip():
        raise PlanInsertionValidationError("idempotency_key is required.")
    if not reason or not reason.strip():
        raise PlanInsertionValidationError("reason is required.")
    if not source_type or not source_ref:
        raise PlanInsertionValidationError("source_type and source_ref are required (provenance is mandatory).")

    # ---- AUTHORITY (checked before the lock -- cheap, and a bad authority_kind should never
    # even contend for the lock another legitimate insertion might be waiting on).
    if authority_kind in NON_AUTHORITATIVE_KINDS or authority_kind not in AUTHORIZED_KINDS:
        raise PlanInsertionAuthorityError(
            f"authority_kind '{authority_kind}' is not sufficient execution authority for a plan insertion."
        )

    tasks_t = tuple(tasks)
    existing_edges_t = tuple(existing_task_dependencies)
    semantic_hash = _semantic_hash(
        authority_kind=authority_kind,
        tasks=tasks_t,
        existing_task_dependencies=existing_edges_t,
        source_type=source_type,
        source_ref=source_ref,
    )

    # ---- LOCK -- everything from here on is serialized per-plan.
    _acquire_plan_insertion_lock(db, plan.id)

    # Re-lock goal and plan rows fresh, INSIDE the advisory lock -- a founder correction or a
    # concurrent supersession must be seen here, not from whatever the caller loaded earlier.
    goal = db.execute(select(MainAIGoal).where(MainAIGoal.id == goal.id).with_for_update()).scalar_one()
    plan = db.execute(select(MainAIPlan).where(MainAIPlan.id == plan.id).with_for_update()).scalar_one()

    if plan.goal_id != goal.id:
        raise PlanInsertionValidationError("plan does not belong to goal.")
    if plan.owner_id != goal.owner_id:
        raise PlanInsertionValidationError("plan/goal owner mismatch.")
    if plan.status != MainAIPlanStatus.active:
        raise PlanInsertionValidationError(f"plan {plan.id} is '{plan.status.value}', not 'active' -- insertion is not allowed.")

    current_hash = hashlib.sha256(goal.original_instruction.encode()).hexdigest()
    if current_hash != authorized_instruction_sha256:
        raise PlanInsertionAuthorityError(
            "goal.original_instruction no longer matches the authorized instruction hash -- "
            "a founder correction superseded this authority; refusing a stale insertion."
        )

    # ---- IDEMPOTENCY REPLAY (inside the lock).
    replay_events = _find_replay(db, plan_id=plan.id, idempotency_key=idempotency_key)
    if replay_events:
        recorded_hash = replay_events[0].detail.get("insertion_semantic_hash")
        if recorded_hash != semantic_hash:
            raise PlanInsertionConflictError(
                f"idempotency_key '{idempotency_key}' was already used for plan {plan.id} with different content."
            )
        task_ids = [uuid.UUID(str(evt.detail["insertion_task_id"])) for evt in replay_events]
        # Preserve original insertion order (recorded on each event) rather than event
        # query-result order, which is not guaranteed to match.
        ordered = sorted(replay_events, key=lambda e: e.detail.get("insertion_task_index", 0))
        task_ids = [uuid.UUID(str(evt.detail["insertion_task_id"])) for evt in ordered]
        canonical = [db.get(MainAITask, tid) for tid in task_ids]
        if any(t is None for t in canonical):
            raise PlanInsertionConflictError(
                f"idempotency_key '{idempotency_key}' replay could not resolve all originally-inserted tasks."
            )
        return canonical  # type: ignore[return-value]

    # ---- FULL VALIDATION (nothing written yet).
    existing_tasks = (
        db.execute(select(MainAITask).where(MainAITask.plan_id == plan.id)).scalars().all()
    )
    existing_by_id = {t.id: t for t in existing_tasks}

    for i, spec in enumerate(tasks_t):
        if not spec.description or not spec.description.strip():
            raise PlanInsertionValidationError(f"tasks[{i}] has an empty description.")
        if spec.task_type not in KNOWN_TASK_TYPES:
            raise PlanInsertionValidationError(f"tasks[{i}]: unknown task_type '{spec.task_type}'.")
        if not 0 <= spec.max_attempts <= 20:
            raise PlanInsertionValidationError(f"tasks[{i}].max_attempts is out of bounds.")
        for step in spec.verification_plan:
            if not isinstance(step, dict) or "kind" not in step:
                raise PlanInsertionValidationError(f"tasks[{i}]: malformed verification_plan entry: {step!r}")
            if step["kind"] == "targeted_tests":
                try:
                    validate_targeted_tests_target(step.get("target"))
                except VerificationStepError as exc:
                    raise PlanInsertionValidationError(str(exc)) from exc
        for dep in spec.depends_on:
            if isinstance(dep, int):
                if not (0 <= dep < len(tasks_t)):
                    raise PlanInsertionValidationError(f"tasks[{i}].depends_on index {dep} is out of range.")
                if dep == i:
                    raise PlanInsertionValidationError(f"tasks[{i}] cannot depend on itself.")
            elif isinstance(dep, uuid.UUID):
                target = existing_by_id.get(dep)
                if target is None:
                    raise PlanInsertionValidationError(
                        f"tasks[{i}].depends_on references task {dep}, which is not an existing task in this plan "
                        "(unknown, cross-owner, or belongs to a different plan)."
                    )
                if target.status in _NEVER_COMPLETES_STATUSES:
                    raise PlanInsertionValidationError(
                        f"tasks[{i}].depends_on references existing task {dep}, which is '{target.status.value}' "
                        "and can never complete."
                    )
            else:
                raise PlanInsertionValidationError(f"tasks[{i}].depends_on entry {dep!r} is neither an int index nor a uuid.")

    locked_existing_by_id: dict[uuid.UUID, MainAITask] = {}
    for j, edge in enumerate(existing_edges_t):
        if not (0 <= edge.depends_on_new_task_index < len(tasks_t)):
            raise PlanInsertionValidationError(
                f"existing_task_dependencies[{j}].depends_on_new_task_index is out of range."
            )
        if edge.existing_task_id not in existing_by_id:
            raise PlanInsertionValidationError(
                f"existing_task_dependencies[{j}] references task {edge.existing_task_id}, which is not an "
                "existing task in this plan (unknown, cross-owner, or belongs to a different plan)."
            )
        # Row-lock the specific existing task before trusting its status -- closes the race
        # where a concurrent recompute_task_readiness()/dispatch_ready_task() promotes it
        # between this check and the dependency row actually being written.
        locked = db.execute(
            select(MainAITask).where(MainAITask.id == edge.existing_task_id).with_for_update()
        ).scalar_one()
        if locked.status not in _SAFE_TO_ADD_PREREQUISITE_STATUSES:
            raise PlanInsertionValidationError(
                f"existing_task_dependencies[{j}]: task {edge.existing_task_id} is '{locked.status.value}' -- "
                "a new prerequisite can only be added to a 'pending' or 'blocked' task, never one already "
                "'ready' (a dispatcher could claim it at any moment), 'running', a wait state, or terminal."
            )
        locked_existing_by_id[edge.existing_task_id] = locked

    # ---- Combined-graph cycle detection over (existing plan tasks) + (new spec indices).
    # Node space: existing tasks keep their real ids as node keys; new specs use their 0-based
    # list index, offset so the two spaces never collide.
    node_index: dict[object, int] = {}
    for existing_id in existing_by_id:
        node_index[existing_id] = len(node_index)
    new_offset = len(node_index)
    for i in range(len(tasks_t)):
        node_index[("new", i)] = new_offset + i
    total_nodes = len(node_index)

    edges: list[tuple[int, int]] = []
    existing_deps = (
        db.execute(select(MainAITaskDependency).where(MainAITaskDependency.task_id.in_(existing_by_id.keys())))
        .scalars()
        .all()
        if existing_by_id
        else []
    )
    for dep_row in existing_deps:
        if dep_row.depends_on_task_id in node_index:
            edges.append((node_index[dep_row.task_id], node_index[dep_row.depends_on_task_id]))
    for i, spec in enumerate(tasks_t):
        for dep in spec.depends_on:
            if isinstance(dep, int):
                edges.append((node_index[("new", i)], node_index[("new", dep)]))
            else:
                edges.append((node_index[("new", i)], node_index[dep]))
    for edge in existing_edges_t:
        edges.append((node_index[edge.existing_task_id], node_index[("new", edge.depends_on_new_task_index)]))

    cycle = _detect_cycle(total_nodes, edges)
    if cycle is not None:
        raise PlanInsertionValidationError(f"Insertion would create a dependency cycle: node path {cycle}.")

    # ---- Everything validated -- persist. Flush only; caller owns the commit (same
    # convention planner.py's create_plan() and executor.py's dispatch_ready_task() follow).
    persisted: list[MainAITask] = []
    for spec in tasks_t:
        task = MainAITask(
            goal_id=goal.id,
            plan_id=plan.id,
            owner_id=goal.owner_id,
            description=spec.description,
            task_type=spec.task_type,
            status=MainAITaskStatus.pending,
            priority=spec.priority,
            risk_level=spec.risk_level,
            approval_required=spec.approval_required,
            verification_plan=list(spec.verification_plan),
            max_attempts=spec.max_attempts,
        )
        db.add(task)
        persisted.append(task)
    db.flush()

    for i, spec in enumerate(tasks_t):
        for dep in spec.depends_on:
            depends_on_task_id = persisted[dep].id if isinstance(dep, int) else dep
            db.add(
                MainAITaskDependency(
                    task_id=persisted[i].id, depends_on_task_id=depends_on_task_id, owner_id=goal.owner_id
                )
            )
    for edge in existing_edges_t:
        db.add(
            MainAITaskDependency(
                task_id=edge.existing_task_id,
                depends_on_task_id=persisted[edge.depends_on_new_task_index].id,
                owner_id=goal.owner_id,
            )
        )

    for i, task in enumerate(persisted):
        db.add(
            MainAITaskEvent(
                task_id=task.id,
                owner_id=task.owner_id,
                event_type=MainAITaskEventType.created,
                detail={
                    "plan_version": plan.version,
                    "insertion_idempotency_key": idempotency_key,
                    "insertion_semantic_hash": semantic_hash,
                    "insertion_task_id": str(task.id),
                    "insertion_task_index": i,
                    "authority_kind": authority_kind,
                    "source_type": source_type,
                    "source_ref": source_ref,
                    "reason": reason,
                    "requested_by": requested_by,
                    "kind": "partial_plan_insertion",
                },
            )
        )

    # Reuses the existing `progress_updated` event vocabulary (no new event_type -- extending
    # migration 0032's mainai_task_events_event_type CHECK constraint would require a new
    # migration for a purely informational annotation) to durably record, on the EXISTING task
    # itself, that it just gained a new prerequisite -- the new MainAITaskDependency row is
    # the actual mechanism; this event is the human/audit-facing narration of why.
    for edge in existing_edges_t:
        existing_task = locked_existing_by_id[edge.existing_task_id]
        db.add(
            MainAITaskEvent(
                task_id=existing_task.id,
                owner_id=existing_task.owner_id,
                event_type=MainAITaskEventType.progress_updated,
                detail={
                    "reason": "partial_plan_insertion_new_prerequisite",
                    "new_prerequisite_task_id": str(persisted[edge.depends_on_new_task_index].id),
                    "insertion_idempotency_key": idempotency_key,
                    "requested_by": requested_by,
                },
            )
        )

    db.flush()
    recompute_task_readiness(db, goal_id=goal.id)

    return persisted
