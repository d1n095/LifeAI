"""AUTONOMOUS GAP -> CHILD-TASK GENERATION -- the primitive that lets MainAI, while executing
an already-authorized goal, turn a REAL, evidence-backed gap (a verification failure, a missing
capability, an absent prerequisite, ...) into a bounded child task, WITHOUT deciding for itself
that the gap deserves work: authority is assessed against the same authorized envelope the
Scoped Development Supervisor already operates under (`SupervisorScope`), and materialization
never touches `mainai_tasks`/`mainai_task_dependencies` directly -- it always hands the proposed
child off to the already-reviewed `app.mainai_execution.plan_insertion.insert_plan_tasks()`
primitive.

This module is deliberately NOT a general autonomous mission generator. Concretely:
  - it never constructs a `MainAIGoal` (see NO TOP-LEVEL GOAL CREATION in the module's own
    tests) -- every gap is scoped to an EXISTING, already-authorized `goal`/`plan`;
  - it never infers authority from provider output -- `evidence_kind` (see
    AUTHORIZED_EVIDENCE_KINDS below) fails closed on anything that is merely an idea, a
    suggestion, or unvalidated provider output ("PROVIDER IDEA != AUTHORITY");
  - it never bypasses `insert_plan_tasks()` -- every accepted gap becomes exactly one call to
    that primitive, inheriting its atomic validation, DAG preservation, idempotency/concurrency
    safety, and approval-policy enforcement for free;
  - it is explicitly bounded (`GapGenerationBounds`) -- no unrestricted recursive expansion.

DURABLE GAP EVIDENCE: reuses `app.problem_learning.service.create_problem()` (LifeProblem +
LifeProblemEvent, migration 0042) rather than inventing a parallel evidence table --
`create_problem()` is already idempotent by `idempotency_key`, already FKs to
`mainai_task_id`/`mainai_job_id`, and already carries a free-form `provenance` JSONB column that
captures every field the founder's GAP RECORD requirement lists (parent goal, source evidence,
exact reason, gap type, affected task/job, repo/path scope, required outcome, authority basis,
unknowns/assumptions, provenance, whether unrelated work can continue) without a new migration.

AUTHORITY ASSESSMENT: replicates the exact deterministic decision tree
`app.development_supervisor.service.validate_scope()` already establishes for "is this scope's
authority still valid" (the `instruction_sha256()` staleness re-check against
`goal.original_instruction`, `AUTHORIZED_KINDS`/`NON_AUTHORITATIVE_KINDS`, capability/risk
envelopes) plus gap-specific checks (self-work gating, unresolved unknowns, multiple candidate
options). `AUTHORIZED_KINDS`/`NON_AUTHORITATIVE_KINDS`/`instruction_sha256()` are duplicated
locally rather than imported, the same deliberate choice `app.mainai_execution.plan_insertion`
and `app.development_supervisor.service` themselves already document -- keeping this module a
peer of, not a dependency of, either the Supervisor or Safe Planner.

IDEMPOTENCY: identical two-layer composition to `plan_insertion.py`'s own idempotency, but
composed rather than reimplemented -- `record_gap()` is idempotent via `create_problem()`'s own
`idempotency_key` (deterministic, derived from the gap's semantic identity), and the
`insert_plan_tasks()` call that follows reuses THAT SAME derived key. Two independent idempotent
primitives composed this way need no additional state of their own: replaying the entire
discover -> record -> assess -> insert pipeline for the exact same gap (whether because the same
gap was independently discovered twice, or because a resume replays it after an interruption)
converges on the same LifeProblem row and the same canonical inserted MainAITask row(s), with no
extra bookkeeping required in this module.

LIVE WIRING: `handle_live_gap_signal()` is the one function `app.development_supervisor.service`'s
`run_supervisor()` loop calls, directly inside its own driver-result handling, to turn a
`VERIFICATION_REQUIRED` or `CAPABILITY_MISSING` driver classification into a gap -- see that
module's own docstring at the call site for exactly which two branches call in and why no others
do. This makes `app.development_supervisor.service` depend on THIS module, which is why
`SupervisorScope`'s shape is duck-typed here (accessed by attribute only, `from __future__ import
annotations` keeps the `SupervisorScope` type hint from ever being evaluated at import time) and
`RISK_ORDER` is duplicated rather than imported -- the reverse import direction (this module
importing FROM development_supervisor.service, as PR #78 originally did) would now be a genuine
circular import."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.checkpoint import record_checkpoint
from app.mainai_execution.plan_insertion import (
    ExistingTaskDependencyEdge,
    InsertedTaskSpec,
    insert_plan_tasks,
)
from app.models.mainai_execution import (
    TERMINAL_MAINAI_GOAL_STATUSES,
    MainAIGoal,
    MainAIGoalRiskLevel,
    MainAIPlan,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
)
from app.models.problem_learning import LifeProblem
from app.problem_learning.service import create_problem

if TYPE_CHECKING:
    from app.development_supervisor.service import SupervisorScope

# Mirrors app/development_supervisor/service.py's identically-named dict -- duplicated, not
# imported, per this module's own "LIVE WIRING" docstring note above.
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

# Mirrors app/safe_planner/service.py's identically-named frozensets (already duplicated a
# second time in app/mainai_execution/plan_insertion.py and a third time in
# app/development_supervisor/service.py, for the identical circular-import-avoidance reason
# each of those modules documents) -- a provider's own output, an idea, a hypothesis can never
# itself grant authority to generate executable work.
AUTHORIZED_KINDS = frozenset({"founder_requirement", "founder_decision", "founder_correction", "authorized_goal"})
NON_AUTHORITATIVE_KINDS = frozenset({"founder_preference", "idea", "suggestion", "hypothesis", "ai_interpretation", "unknown"})

# What counts as REAL evidence a gap exists -- deliberately excludes anything that is merely a
# provider's own suggestion/idea/hypothesis ("PROVIDER IDEA != AUTHORITY"). A provider MAY
# surface a candidate step ("provider_identified_step_validated"), but only after independent,
# deterministic validation the CALLER performed (verifying the suggested capability really is
# missing, the suggested prerequisite really is absent, etc.) -- this module has no way to
# perform that validation itself, the same "structurally required, not independently
# re-verified" boundary insert_plan_tasks() already establishes for authority_kind.
AUTHORIZED_EVIDENCE_KINDS = frozenset(
    {
        "verification_failure",
        "capability_missing",
        "missing_prerequisite",
        "unresolved_dependency",
        "repository_inspection",
        "provider_identified_step_validated",
    }
)

# A gap of one of these types is, by definition, about MainAI's own capabilities/development
# independence rather than ordinary repair/repro work on the parent goal's own subject matter --
# it may only be authorized under a scope that explicitly opts into self-work
# (SupervisorScope.self_work=True), never implicitly inherited from an ordinary development scope.
_SELF_WORK_GAP_TYPES = frozenset({"capability_missing", "self_improvement"})

DEFAULT_MAX_GENERATION_DEPTH = 3


class GapGenerationError(Exception):
    """Base for every error this module raises."""


class GapEvidenceError(GapGenerationError):
    """Fail-closed: `evidence_kind` is not one of AUTHORIZED_EVIDENCE_KINDS -- an idea,
    suggestion, hypothesis, or unvalidated provider output can never itself become a recorded,
    executable gap. Nothing is written."""


@dataclass(frozen=True)
class DiscoveredGap:
    """One candidate gap, already reduced to the deterministic facts this module needs --
    constructing this object IS the "preserve evidence" step; the caller is responsible for
    populating it from real, already-durable evidence (a `verification_failed`
    MainAITaskEvent, a `capability_missing` MainAICheckpoint/WorkTraceEvent, a
    LifeIntentBlocker, ...), never from a provider's raw prose."""

    gap_type: str
    evidence_kind: str
    parent_goal_id: uuid.UUID
    reason: str
    required_outcome: str
    task_type: str = "repo_edit"
    source_task_id: uuid.UUID | None = None
    source_job_id: uuid.UUID | None = None
    repository_identity: str = ""
    allowed_paths: tuple[str, ...] = field(default_factory=tuple)
    required_capabilities: tuple[str, ...] = field(default_factory=tuple)
    risk_level: MainAIGoalRiskLevel = MainAIGoalRiskLevel.low
    verification_plan: tuple[dict, ...] = field(default_factory=tuple)
    # Unresolved questions that must be answered by a founder before this gap can become
    # executable work -- non-empty forces NEEDS_CLARIFICATION, never a guess.
    unknowns: tuple[str, ...] = field(default_factory=tuple)
    # More than one equally-plausible remediation -- forces NEEDS_SELECTION rather than this
    # module silently picking one.
    candidate_options: tuple[str, ...] = field(default_factory=tuple)
    unrelated_work_can_continue: bool = True
    # An existing task (other than a NEVER_COMPLETES one) the new child should wait on --
    # deliberately excludes the gap's OWN source_task_id when that task is `failed`/`cancelled`
    # (insert_plan_tasks() would reject that dependency outright; a repair child is independent
    # new work, not a continuation of the failed attempt).
    depends_on_existing_task_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    # MISSING PREREQUISITE gaps only: the existing task that should gain a new dependency on
    # this gap's proposed child (purely additive -- see plan_insertion.py's own docstring).
    existing_task_dependency_target: uuid.UUID | None = None
    generation_depth: int = 0
    source_type: str = "autonomous_gap_discovery"
    source_ref: str = ""


@dataclass(frozen=True)
class GapOutcome:
    """`classification` is `"ACCEPTED"` (a child task was inserted), `"DEPTH_BOUND_REACHED"`
    (bounds, not authority), or one of the six authority-assessment terminal states the founder's
    spec requires: NEEDS_AUTHORIZATION / NEEDS_CLARIFICATION / NEEDS_SELECTION / OUT_OF_SCOPE /
    EXTERNAL_REVIEW_REQUIRED / CAPABILITY_MISSING. `problem` is always set (the gap is always
    durably recorded, whether or not it becomes executable work) unless evidence itself was
    rejected (GapEvidenceError raised before this outcome could even be constructed)."""

    classification: str
    problem: LifeProblem | None
    inserted_tasks: list[MainAITask] | None
    reason: str


@dataclass(frozen=True)
class GapGenerationBounds:
    """No unbounded recursive expansion. `max_generation_depth` bounds how many gap ->
    child -> (that child later reveals another gap) generations deep a lineage may go --
    enforced per-gap via `DiscoveredGap.generation_depth`, not by this module driving recursion
    itself (this module never re-invokes itself automatically; a caller who discovers a further
    gap while executing a generated child constructs a new `DiscoveredGap` with
    `generation_depth = parent.generation_depth + 1` and calls back in, same as any other gap)."""

    max_gaps_per_run: int = 10
    max_children_per_run: int = 10
    max_generation_depth: int = DEFAULT_MAX_GENERATION_DEPTH
    max_elapsed_seconds: int = 300
    max_unresolved_gaps: int = 20


@dataclass(frozen=True)
class GapGenerationRunResult:
    outcomes: list[GapOutcome]
    # None if every supplied gap was processed; otherwise which bound stopped the run --
    # resumable by re-calling run_gap_generation with the remaining (unprocessed) gaps.
    stopped_reason: str | None


def instruction_sha256(instruction: str) -> str:
    """Identical one-line idiom to app.development_supervisor.service.instruction_sha256() --
    duplicated, not imported, for the same reason AUTHORIZED_KINDS is duplicated above."""
    return hashlib.sha256(instruction.encode()).hexdigest()


def _gap_identity_key(gap: DiscoveredGap) -> str:
    """Canonical content hash of a gap's SEMANTIC identity -- deliberately excludes
    `required_capabilities`/`verification_plan`/`risk_level`/timestamps (incidental proposal
    detail, not what makes two discoveries "the same gap"), same exclusion principle
    plan_insertion.py's `_semantic_hash()` applies. Two DiscoveredGap constructions with the
    same identity are the SAME gap -- this is what makes "duplicate gap discovered twice"
    converge on one LifeProblem and one inserted task."""
    payload = {
        "gap_type": gap.gap_type,
        "parent_goal_id": str(gap.parent_goal_id),
        "source_task_id": str(gap.source_task_id) if gap.source_task_id else None,
        "source_job_id": str(gap.source_job_id) if gap.source_job_id else None,
        "reason": gap.reason,
        "required_outcome": gap.required_outcome,
        "existing_task_dependency_target": (
            str(gap.existing_task_dependency_target) if gap.existing_task_dependency_target else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def record_gap(db: Session, *, owner_id: uuid.UUID, gap: DiscoveredGap, requested_by: str) -> LifeProblem:
    """Durably records `gap` as a `LifeProblem` (migration 0042) -- reusing the EXISTING
    Problem/Solution/Decision evidence structures rather than a parallel one, exactly as the
    founder's GAP RECORD requirement asks. Idempotent via `create_problem()`'s own
    `idempotency_key` mechanism: recording the SAME semantic gap twice (or replaying this call
    after an interruption) returns the SAME row, never a duplicate.

    Fails closed (GapEvidenceError, nothing written) if `gap.evidence_kind` is not one of
    AUTHORIZED_EVIDENCE_KINDS -- this is the "no provider output grants authority" boundary."""
    if gap.evidence_kind not in AUTHORIZED_EVIDENCE_KINDS:
        raise GapEvidenceError(
            f"evidence_kind '{gap.evidence_kind}' is not sufficient evidence to record an executable gap "
            "-- an idea, suggestion, hypothesis, or unvalidated provider output can never itself "
            "become a recorded gap."
        )
    idempotency_key = f"autonomous_gap:{_gap_identity_key(gap)}"
    provenance = {
        "gap_type": gap.gap_type,
        "evidence_kind": gap.evidence_kind,
        "required_outcome": gap.required_outcome,
        "repository_identity": gap.repository_identity,
        "allowed_paths": list(gap.allowed_paths),
        "required_capabilities": list(gap.required_capabilities),
        "risk_level": gap.risk_level.value,
        "unknowns": list(gap.unknowns),
        "candidate_options": list(gap.candidate_options),
        "unrelated_work_can_continue": gap.unrelated_work_can_continue,
        "generation_depth": gap.generation_depth,
        "source_type": gap.source_type,
        "source_ref": gap.source_ref,
        "requested_by": requested_by,
    }
    return create_problem(
        db,
        owner_id=owner_id,
        title=f"Autonomous gap ({gap.gap_type}): {gap.required_outcome[:180]}",
        description=gap.reason,
        idempotency_key=idempotency_key,
        status="open",
        classification_basis="deterministic",
        authority="deterministic_source",
        provenance=provenance,
        life_intent_id=None,
        mainai_task_id=gap.source_task_id,
        mainai_job_id=gap.source_job_id,
    )


def assess_gap_authority(db: Session, *, scope: SupervisorScope, goal: MainAIGoal, gap: DiscoveredGap) -> tuple[str | None, str]:
    """Deterministically decides whether `scope`'s existing founder authorization covers turning
    `gap` into executable work. Returns `(None, reason)` when authorized; otherwise
    `(classification, reason)` where `classification` is one of the six terminal states the
    founder's AUTHORITY ASSESSMENT section requires. Never guesses -- every branch below is a
    concrete, deterministic check against `scope`/`goal`/`gap`, none of it inferred from
    provider output.

    Automatic child-task authorization requires ALL of: parent goal currently founder-authorized
    (authority_kind valid, instruction hash live -- not stale behind a founder correction); goal
    not terminal; gap's repository/path scope within `scope`'s; self-work gates honored; gap's
    required capabilities within `scope`'s envelope; gap's risk within `scope`'s envelope AND
    never `high` regardless of envelope (external review is always required for that); no
    unresolved unknowns; no unresolved multi-option ambiguity; explicit completion criteria."""
    if scope.authority_kind in NON_AUTHORITATIVE_KINDS or scope.authority_kind not in AUTHORIZED_KINDS:
        return "NEEDS_AUTHORIZATION", f"authority_kind '{scope.authority_kind}' is not sufficient execution authority for autonomous gap remediation."
    if instruction_sha256(goal.original_instruction) != scope.authorized_instruction_sha256:
        return (
            "NEEDS_AUTHORIZATION",
            "goal.original_instruction no longer matches the authorized instruction hash -- a founder "
            "correction superseded this authority; refusing a stale gap-derived task generation.",
        )
    if goal.status in TERMINAL_MAINAI_GOAL_STATUSES:
        return "OUT_OF_SCOPE", f"goal {goal.id} is '{goal.status.value}' (terminal) -- no further work can be authorized under it."
    if gap.repository_identity != scope.repository_identity:
        return "OUT_OF_SCOPE", f"gap's repository_identity '{gap.repository_identity}' is outside the authorized scope '{scope.repository_identity}'."
    if not set(gap.allowed_paths).issubset(set(scope.allowed_paths)):
        return "OUT_OF_SCOPE", "gap's path scope is not a subset of the authorized scope's allowed_paths."
    if gap.gap_type in _SELF_WORK_GAP_TYPES and not scope.self_work:
        return (
            "NEEDS_AUTHORIZATION",
            f"gap_type '{gap.gap_type}' concerns MainAI's own capabilities/development independence, which "
            "requires an explicitly self-work-authorized scope (SupervisorScope.self_work=True); this scope "
            "does not grant it.",
        )
    if set(gap.required_capabilities) - set(scope.allowed_capabilities):
        return "CAPABILITY_MISSING", "remediating this gap requires a capability outside the authorized capability envelope."
    if RISK_ORDER.get(gap.risk_level.value, 99) > RISK_ORDER.get(scope.maximum_risk, -1):
        return "NEEDS_AUTHORIZATION", f"gap's risk level '{gap.risk_level.value}' exceeds the authorized risk envelope '{scope.maximum_risk}'."
    if gap.risk_level == MainAIGoalRiskLevel.high:
        return "EXTERNAL_REVIEW_REQUIRED", "high-risk remediation always requires external/human review, regardless of the authorized envelope."
    if gap.unknowns:
        return "NEEDS_CLARIFICATION", f"unresolved unknowns block automatic authorization: {'; '.join(gap.unknowns)}"
    if len(gap.candidate_options) > 1:
        return "NEEDS_SELECTION", f"multiple viable remediation options require founder selection: {'; '.join(gap.candidate_options)}"
    if not gap.required_outcome.strip():
        return "NEEDS_CLARIFICATION", "no explicit completion criteria supplied for this gap's remediation."
    return (
        None,
        "gap is authorized: parent goal is currently founder-authorized, repository/path/capability/risk "
        "envelopes hold, and no founder correction supersedes the direction.",
    )


def propose_child_task_spec(gap: DiscoveredGap) -> InsertedTaskSpec:
    """Builds the bounded `InsertedTaskSpec` insert_plan_tasks() itself validates -- this
    function makes no decision about WHETHER the gap should become work (that already happened
    in assess_gap_authority()); it only shapes what the resulting task looks like."""
    return InsertedTaskSpec(
        description=gap.required_outcome,
        task_type=gap.task_type,
        depends_on=tuple(gap.depends_on_existing_task_ids),
        risk_level=gap.risk_level,
        approval_required=False,
        verification_plan=gap.verification_plan,
        priority=0,
        max_attempts=3,
    )


def generate_child_task_for_gap(
    db: Session,
    *,
    scope: SupervisorScope,
    goal: MainAIGoal,
    plan: MainAIPlan,
    gap: DiscoveredGap,
    requested_by: str,
    max_generation_depth: int = DEFAULT_MAX_GENERATION_DEPTH,
) -> GapOutcome:
    """The atomic per-gap pipeline: record evidence (always) -> depth bound -> authority
    assessment -> (if authorized) propose + insert via insert_plan_tasks(). Never writes a
    `mainai_tasks` row directly -- the only mutation path to that table is the call to
    insert_plan_tasks() below. Never constructs a MainAIGoal. Idempotent end to end (see this
    module's own docstring) -- safe to call twice for the same gap, and safe to resume after an
    interruption at any point."""
    problem = record_gap(db, owner_id=goal.owner_id, gap=gap, requested_by=requested_by)

    if gap.generation_depth >= max_generation_depth:
        return GapOutcome(
            "DEPTH_BOUND_REACHED",
            problem,
            None,
            f"generation depth {gap.generation_depth} reached the configured bound {max_generation_depth} -- "
            "no further recursive gap-generation is permitted for this lineage.",
        )

    classification, reason = assess_gap_authority(db, scope=scope, goal=goal, gap=gap)
    if classification is not None:
        return GapOutcome(classification, problem, None, reason)

    task_spec = propose_child_task_spec(gap)
    existing_edges = []
    if gap.existing_task_dependency_target is not None:
        existing_edges = [ExistingTaskDependencyEdge(existing_task_id=gap.existing_task_dependency_target, depends_on_new_task_index=0)]

    child_idempotency_key = f"autonomous_gap_child:{problem.idempotency_key}"
    inserted = insert_plan_tasks(
        db,
        goal=goal,
        plan=plan,
        authority_kind=scope.authority_kind,
        authorized_instruction_sha256=scope.authorized_instruction_sha256,
        idempotency_key=child_idempotency_key,
        tasks=[task_spec],
        existing_task_dependencies=existing_edges,
        source_type=gap.source_type,
        source_ref=gap.source_ref or f"autonomous_gap:{gap.gap_type}:{problem.id}",
        reason=gap.reason,
        requested_by=requested_by,
    )
    return GapOutcome("ACCEPTED", problem, inserted, "child task inserted via the partial-plan-insertion primitive.")


def run_gap_generation(
    db: Session,
    *,
    scope: SupervisorScope,
    goal: MainAIGoal,
    plan: MainAIPlan,
    gaps: list[DiscoveredGap],
    bounds: GapGenerationBounds,
    requested_by: str,
    run_id: uuid.UUID,
) -> GapGenerationRunResult:
    """Processes `gaps` in order, each through generate_child_task_for_gap(), stopping
    resumably (never mid-gap -- each gap is processed atomically) the moment any bound in
    `bounds` is reached. A CAPABILITY_MISSING/NEEDS_*/OUT_OF_SCOPE outcome for one gap never
    stops processing of the next -- "preserve gap A as waiting, continue gap B" is the default
    behavior, not a special case.

    Checkpoints (MainAICheckpoint, step="autonomous_gap_generation", keyed by `run_id` as the
    checkpoint's job_id) after every processed gap that has a real `source_task_id` to attach
    the checkpoint to -- durable proof of run progress. Actual correctness under interruption
    does NOT depend on these checkpoints (record_gap()/insert_plan_tasks() are independently
    idempotent -- see this module's own docstring); they exist for observability and to let a
    caller resume a partially-completed run by re-supplying only the unprocessed tail of
    `gaps` without needing to re-derive which gaps already landed."""
    started = time.monotonic()
    outcomes: list[GapOutcome] = []
    accepted = 0
    unresolved = 0
    stopped_reason: str | None = None

    for index, gap in enumerate(gaps):
        if index >= bounds.max_gaps_per_run:
            stopped_reason = "GAPS_BOUND_REACHED"
            break
        if time.monotonic() - started >= bounds.max_elapsed_seconds:
            stopped_reason = "TIME_BOUND_REACHED"
            break
        if unresolved >= bounds.max_unresolved_gaps:
            stopped_reason = "UNRESOLVED_BOUND_REACHED"
            break

        outcome = generate_child_task_for_gap(
            db,
            scope=scope,
            goal=goal,
            plan=plan,
            gap=gap,
            requested_by=requested_by,
            max_generation_depth=bounds.max_generation_depth,
        )
        outcomes.append(outcome)
        if outcome.classification == "ACCEPTED":
            accepted += 1
        else:
            unresolved += 1

        if gap.source_task_id is not None:
            source_task = db.get(MainAITask, gap.source_task_id)
            if source_task is not None:
                record_checkpoint(
                    db,
                    task=source_task,
                    goal=goal,
                    job_id=run_id,
                    step="autonomous_gap_generation",
                    data={
                        "gap_index": index,
                        "classification": outcome.classification,
                        "problem_id": str(outcome.problem.id) if outcome.problem else None,
                    },
                )

        if accepted >= bounds.max_children_per_run:
            stopped_reason = "CHILDREN_BOUND_REACHED"
            break

    return GapGenerationRunResult(outcomes=outcomes, stopped_reason=stopped_reason)


# ================================================================== LIVE WIRING
#
# The only two driver-result classifications app.development_supervisor.service.run_supervisor()
# calls into this module for -- see that module's own call site for the exact branches. No other
# classification (WAITING_PROVIDER, WAITING_APPROVAL, PROVIDER_SPEND_NOT_AUTHORIZED, NEEDS_*,
# OUT_OF_SCOPE, RUN_BOUND_REACHED, ...) is a structured, durable gap signal -- those are either
# transient waits a retry can resolve on its own, or terminal states that already carry their own
# explanation and require no further action from this module.
LIVE_GAP_SIGNAL_CLASSIFICATIONS = frozenset({"VERIFICATION_REQUIRED", "CAPABILITY_MISSING"})


def _live_generation_depth(db: Session, *, task: MainAITask) -> int:
    """`gap_from_verification_required()`/`gap_from_capability_missing()` build a fresh
    `DiscoveredGap` from `task` alone, with no lineage of their own -- without this lookup every
    live-detected gap would default to `generation_depth=0` even when `task` IS ITSELF a
    previously gap-generated repair/capability child, letting a repeated live gap chain (a
    repair child that later fails verification again, generating its own repair, ...) run
    forever, never reaching `GapGenerationBounds`/`DEFAULT_MAX_GENERATION_DEPTH`.

    Derives depth from data this module already writes durably -- no new column, no migration:
    a gap-generated task's own `created` MainAITaskEvent carries the
    `autonomous_gap_child:<problem idempotency_key>` `insertion_idempotency_key`
    `insert_plan_tasks()` stores for every task it creates (see plan_insertion.py's own
    docstring); stripping the `autonomous_gap_child:` prefix recovers the parent LifeProblem's
    `idempotency_key`, and that problem's own `provenance["generation_depth"]` (set by
    `record_gap()`) is this task's ancestor depth. An ordinary, non-gap-generated task (no such
    prefix, or no `created` event at all -- should not happen but fails safe) is depth 0."""
    prefix = "autonomous_gap_child:"
    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return 0
    key = (event.detail or {}).get("insertion_idempotency_key", "")
    if not key.startswith(prefix):
        return 0
    parent_problem = db.execute(
        select(LifeProblem).where(LifeProblem.idempotency_key == key[len(prefix):])
    ).scalar_one_or_none()
    if parent_problem is None:
        return 0
    return int((parent_problem.provenance or {}).get("generation_depth", 0)) + 1


def gap_from_verification_required(*, db: Session, scope: SupervisorScope, goal: MainAIGoal, task: MainAITask) -> DiscoveredGap:
    """The live counterpart to this module's own `verification_failure` evidence_kind -- built
    from the REAL classification `app.development_driver.service.run_driver()` already returned,
    never from raw logs or provider prose. The repair child is independent new work: it does not
    depend on `task` itself, since `task` may be `failed`/terminal by the time this runs, and
    `insert_plan_tasks()` refuses a dependency on a task that can never complete.

    Deliberately excludes anything that varies between retries of the SAME underlying failure
    (e.g. `run_driver()`'s own per-attempt `driver.checkpoint_id`) from every field that feeds
    `record_gap()`'s idempotency identity (`reason`/`required_outcome`/`source_ref` -- see
    `_gap_identity_key()`) -- content here is a pure function of `(task.id, task.description)`,
    so a stale worker retrying the identical task after a lease loss, or the Supervisor being
    invoked again after a crash, converges on the exact same LifeProblem/child rather than
    minting a new one each time. `create_problem()`'s own idempotency check would otherwise
    reject a replay whose `provenance` differs from the first call as a semantic conflict
    (`ProblemLearningError`), not silently accept it."""
    return DiscoveredGap(
        gap_type="verification_failure",
        evidence_kind="verification_failure",
        parent_goal_id=goal.id,
        reason=f"Task {task.id} ('{task.description}') did not reach verified completion; bounded repair required.",
        required_outcome=f"Repair the verification failure blocking: {task.description}",
        task_type="repo_edit",
        source_task_id=task.id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        risk_level=task.risk_level,
        source_type="mainai_supervisor_verification_required",
        source_ref=f"mainai_task:{task.id}:verification_required",
        generation_depth=_live_generation_depth(db, task=task),
    )


def gap_from_capability_missing(*, db: Session, scope: SupervisorScope, goal: MainAIGoal, task: MainAITask, capability: str) -> DiscoveredGap:
    """The live counterpart to this module's own `capability_missing` evidence_kind -- built
    from the REAL `capability` name `run_driver()` already reported (surfaced by
    `app.development_operator.service`'s `OperatorCapabilityMissing`/`capability_missing()`),
    never from raw logs or provider prose. `required_capabilities` is deliberately left empty:
    it names capabilities the REMEDIATION needs (ordinary repo-edit capabilities, already within
    any properly-scoped goal's envelope), not the missing capability itself -- the child's whole
    job is to ADD that capability, so requiring it as a precondition would be circular.

    Same retry-stability property as `gap_from_verification_required()` above -- content is a
    pure function of `(task.id, task.description, capability)`, nothing per-attempt."""
    return DiscoveredGap(
        gap_type="capability_missing",
        evidence_kind="capability_missing",
        parent_goal_id=goal.id,
        reason=f"Execution of task {task.id} ('{task.description}') reported a missing capability: {capability}.",
        required_outcome=f"Add deterministic support for the missing capability: {capability}",
        task_type="repo_edit",
        source_task_id=task.id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        risk_level=MainAIGoalRiskLevel.medium,
        source_type="mainai_supervisor_capability_missing",
        source_ref=f"mainai_task:{task.id}:capability_missing:{capability}",
        generation_depth=_live_generation_depth(db, task=task),
    )


def handle_live_gap_signal(
    db: Session,
    *,
    scope: SupervisorScope,
    goal: MainAIGoal,
    plan: MainAIPlan,
    task: MainAITask,
    classification: str,
    capability: str | None = None,
    requested_by: str,
) -> GapOutcome | None:
    """The one live-wiring entry point `run_supervisor()` calls, directly inside its own
    driver-result handling. Given a driver-result `classification` the Supervisor just received,
    decides whether it is one of the recognized, structured gap signals
    (`LIVE_GAP_SIGNAL_CLASSIFICATIONS`) and -- if so -- runs the FULL
    record_gap -> assess_gap_authority -> insert_plan_tasks chain via
    `generate_child_task_for_gap()`, inheriting every guarantee that function already has
    (idempotent, concurrency-safe, fails closed on stale/insufficient authority, never bypasses
    insert_plan_tasks(), never creates a MainAIGoal).

    Returns `None` for any classification not in `LIVE_GAP_SIGNAL_CLASSIFICATIONS` -- the
    caller's existing behavior for every other classification (WAITING_PROVIDER,
    WAITING_APPROVAL, NEEDS_*, OUT_OF_SCOPE, ...) is completely unchanged; this function is
    purely additive at its one call site, never a replacement for existing branches."""
    if classification not in LIVE_GAP_SIGNAL_CLASSIFICATIONS:
        return None
    if classification == "VERIFICATION_REQUIRED":
        gap = gap_from_verification_required(db=db, scope=scope, goal=goal, task=task)
    else:
        gap = gap_from_capability_missing(db=db, scope=scope, goal=goal, task=task, capability=capability or "unknown")
    return generate_child_task_for_gap(db, scope=scope, goal=goal, plan=plan, gap=gap, requested_by=requested_by)
