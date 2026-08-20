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

from datetime import datetime

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
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.problem_learning import LifeProblem, LifeProblemEvent
from app.problem_learning.service import create_problem

if TYPE_CHECKING:
    from app.development_operator.service import OperatorContext
    from app.development_supervisor.service import SupervisorScope, WorkBinding

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


class GapLeaseLostError(GapGenerationError):
    """Fail-closed: the worker lease was lost or fenced out before durable gap recording /
    child insertion. Nothing may be inserted under a stale lease."""


class GapLineageError(GapGenerationError):
    """Fail-closed: a gap-generated child's lineage cannot be proven owner-scoped. Depth must
    never silently collapse to 0 for an unproven autonomous_gap_child lineage."""


class GapCapabilityError(GapGenerationError):
    """Fail-closed: a CAPABILITY_MISSING signal arrived without a concrete capability name.
    Never collapse to a synthetic 'unknown' capability."""


# Attempt-specific fields that must NOT participate in LifeProblem semantic identity /
# create_problem() provenance equality. They are recorded as LifeProblemEvent detail instead
# so worker takeover with a different worker_id converges on the same canonical gap.
_ATTEMPT_PROVENANCE_KEYS = frozenset({"requested_by", "attempt_worker_id", "attempt_at"})

# Driver / planner classifications that are structured, durable gap signals for live wiring.
LIVE_GAP_SIGNAL_CLASSIFICATIONS = frozenset(
    {"VERIFICATION_REQUIRED", "CAPABILITY_MISSING", "FAILED_NONRETRYABLE"}
)

GAP_CHILD_INSERTION_PREFIX = "autonomous_gap_child:"


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
    # Structured execution envelope for the inserted child -- enough for the Supervisor to
    # derive WorkBinding / request Safe Planner without founder/test job translation, and
    # without synthesizing executable steps from free-form provider prose.
    execution_envelope: dict = field(default_factory=dict)
    # Structured verification / operator failure evidence (never free-form logs alone).
    failure_evidence: dict = field(default_factory=dict)


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


def _record_gap_attempt_event(db: Session, *, problem: LifeProblem, requested_by: str) -> None:
    """Attempt-specific provenance lives on LifeProblemEvent, not LifeProblem.provenance, so
    semantic gap identity survives worker takeover with a different worker_id.

    Uses the existing allowed event_type `outcome_recorded` (migration 0042 CHECK) with a
    structured detail discriminator -- no new migration required."""
    db.add(
        LifeProblemEvent(
            owner_id=problem.owner_id,
            problem_id=problem.id,
            event_type="outcome_recorded",
            detail={"gap_attempt": True, "requested_by": requested_by},
            actor_type="system",
        )
    )
    db.flush()


def record_gap(db: Session, *, owner_id: uuid.UUID, gap: DiscoveredGap, requested_by: str) -> LifeProblem:
    """Durably records `gap` as a `LifeProblem` (migration 0042) -- reusing the EXISTING
    Problem/Solution/Decision evidence structures rather than a parallel one, exactly as the
    founder's GAP RECORD requirement asks. Idempotent via `create_problem()`'s own
    `idempotency_key` mechanism: recording the SAME semantic gap twice (or replaying this call
    after an interruption) returns the SAME row, never a duplicate.

    Fails closed (GapEvidenceError, nothing written) if `gap.evidence_kind` is not one of
    AUTHORIZED_EVIDENCE_KINDS -- this is the "no provider output grants authority" boundary.

    `requested_by` is deliberately excluded from LifeProblem.provenance (see
    `_ATTEMPT_PROVENANCE_KEYS`) and recorded as a LifeProblemEvent instead -- otherwise a
    different-worker takeover would raise ProblemLearningError on provenance inequality even
    though the semantic gap is identical."""
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
        "execution_envelope": dict(gap.execution_envelope or {}),
        "failure_evidence": dict(gap.failure_evidence or {}),
        "parent_goal_id": str(gap.parent_goal_id),
        "source_task_id": str(gap.source_task_id) if gap.source_task_id else None,
        "source_job_id": str(gap.source_job_id) if gap.source_job_id else None,
    }
    for key in _ATTEMPT_PROVENANCE_KEYS:
        provenance.pop(key, None)
    problem = create_problem(
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
    _record_gap_attempt_event(db, problem=problem, requested_by=requested_by)
    return problem


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
    operator_context: OperatorContext | None = None,
) -> GapOutcome:
    """The atomic per-gap pipeline: record evidence (always) -> depth bound -> authority
    assessment -> (if authorized) propose + insert via insert_plan_tasks(). Never writes a
    `mainai_tasks` row directly -- the only mutation path to that table is the call to
    insert_plan_tasks() below. Never constructs a MainAIGoal. Idempotent end to end (see this
    module's own docstring) -- safe to call twice for the same gap, and safe to resume after an
    interruption at any point.

    When `operator_context` is supplied, the MainAIJob row is locked with
    `SELECT … FOR UPDATE` and lease/generation ownership is revalidated before ANY durable
    gap/child write. A stale worker fails closed with GapLeaseLostError and inserts nothing."""
    if operator_context is not None:
        require_live_gap_lease(db, context=operator_context, for_update=True)

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

    if operator_context is not None:
        # Still holding the same transaction lock; re-assert generation before child insert.
        require_live_gap_lease(db, context=operator_context, for_update=True)

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
# Classifications in LIVE_GAP_SIGNAL_CLASSIFICATIONS (defined near AUTHORIZED_EVIDENCE_KINDS)
# are the structured, durable gap signals app.development_supervisor.service.run_supervisor()
# calls into this module for -- post-driver AND Safe Planner pre-driver CAPABILITY_MISSING.
# Transient waits and terminal admin states are not gap signals.


def require_live_gap_lease(
    db: Session, *, context: OperatorContext, for_update: bool = False
) -> MainAIJob:
    """Revalidate worker lease / locked_by / generation fencing for live gap work.

    When `for_update=True`, the MainAIJob row is locked (`SELECT … FOR UPDATE`) and the
    caller must hold that lock through durable gap/child writes so a concurrent takeover
    cannot slip between an unlocked preflight check and `record_gap`/`insert_plan_tasks`.
    A stale worker must fail closed and never insert work."""
    stmt = select(MainAIJob).where(
        MainAIJob.id == context.job_id,
        MainAIJob.owner_id == context.owner_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    job = db.execute(stmt).scalar_one_or_none()
    if (
        job is None
        or job.status != MainAIJobStatus.running
        or job.locked_by != context.worker_id
        or job.lease_generation != context.lease_generation
    ):
        raise GapLeaseLostError("stale or absent MainAI job lease before gap recording/insertion")
    if job.lease_expires_at is not None and job.lease_expires_at <= datetime.utcnow():
        raise GapLeaseLostError("expired MainAI job lease before gap recording/insertion")
    return job


def _intersect_paths(*path_sets: tuple[str, ...]) -> tuple[str, ...]:
    """Narrowest valid intersection of path envelopes. Empty intersection fails closed upstream."""
    if not path_sets:
        return ()
    current = set(path_sets[0])
    for paths in path_sets[1:]:
        if paths:
            current &= set(paths)
    return tuple(sorted(current))


def _narrow_allowed_paths(*, scope: SupervisorScope, binding_paths: tuple[str, ...] | None) -> tuple[str, ...]:
    if binding_paths:
        return _intersect_paths(tuple(scope.allowed_paths), tuple(binding_paths))
    return tuple(scope.allowed_paths)


def _structured_repair_recipe_for_gap(
    *,
    goal: MainAIGoal,
    failure_evidence: dict | None,
    allowed_paths: tuple[str, ...],
) -> str | None:
    """Attach a *registered demo* repair recipe only when structured failure evidence qualifies.

    `multiplication_repair` proves the live WorkBinding/PlanCandidate handoff for the bounded
    calculator fixture — it is NOT general arbitrary-code repair. Eligibility requires all of:
      1. verification/test-shaped failure evidence (not free-form prose alone),
      2. the authorized goal instruction naming the calculator multiplication domain,
      3. the effective path envelope already authorizing both recipe paths.
    Vague instruction text alone never selects a recipe.
    """
    evidence = failure_evidence or {}
    failed_capability = str(
        evidence.get("failed_capability") or evidence.get("capability") or ""
    ).strip()
    if failed_capability not in {
        "run_focused_test",
        "run_static_check",
        "verification_evaluate",
        "patch_file",
        "create_file",
    } and not evidence.get("verification_required"):
        return None
    instruction = (goal.original_instruction or "").lower()
    if "calculator" not in instruction or (
        "multiplication" not in instruction and "multiply" not in instruction
    ):
        return None
    required_paths = {"calculator.py", "test_calculator.py"}
    if not required_paths.issubset(set(allowed_paths)):
        # Envelope too narrow for this demo recipe — do not attach; planning must fail closed.
        return None
    step_args = evidence.get("step_arguments") or {}
    test_args = step_args.get("arguments") if isinstance(step_args, dict) else None
    if isinstance(test_args, list) and test_args:
        if "test_calculator.py" not in {str(item) for item in test_args}:
            return None
    return "multiplication_repair"


def _reverify_contract_from_evidence(
    *, task: MainAITask, failure_evidence: dict | None
) -> dict | None:
    """Structured re-verification contract for the ORIGINAL source task after repair.

    Prefer explicit focused-test arguments from the failing driver step, then the task's own
    verification_plan targeted_tests entries. Returns None when no supported contract exists
    (caller must fail closed rather than invent test_calculator.py)."""
    evidence = failure_evidence or {}
    step_args = evidence.get("step_arguments") if isinstance(evidence.get("step_arguments"), dict) else {}
    if evidence.get("failed_capability") == "run_focused_test" or step_args.get("profile_name"):
        arguments = step_args.get("arguments")
        profile = step_args.get("profile_name") or "focused_pytest"
        if isinstance(arguments, list) and arguments:
            return {
                "capability": "run_focused_test",
                "profile_name": profile,
                "arguments": [str(item) for item in arguments],
                "source": "failure_evidence.step_arguments",
            }
    for entry in task.verification_plan or ():
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") == "targeted_tests" and entry.get("target"):
            return {
                "capability": "run_focused_test",
                "profile_name": "focused_pytest",
                "arguments": [str(entry["target"])],
                "source": "task.verification_plan",
            }
        if entry.get("kind") == "static_analysis":
            return {
                "capability": "run_static_check",
                "profile_name": "static",
                "arguments": [],
                "source": "task.verification_plan",
            }
    return None


def _build_execution_envelope(
    *,
    scope: SupervisorScope,
    task: MainAITask,
    allowed_paths: tuple[str, ...],
    gap_type: str,
    requested_capability: str | None = None,
    required_capabilities: tuple[str, ...] = (),
    failure_evidence: dict | None = None,
    source_job_id: uuid.UUID | None = None,
    generation_depth: int = 0,
    repair_recipe: str | None = None,
    reverify: dict | None = None,
) -> dict:
    """Structured envelope persisted on LifeProblem.provenance so the Supervisor can derive a
    WorkBinding without a founder/test hand-authored PlanCandidate. Never auto-approved.
    Concrete PlanCandidate hashes are resolved later from `repair_recipe` + live operator
    context so attempt/takeover provenance stays semantically stable."""
    return {
        "plan_mode": "structured_envelope",
        "gap_type": gap_type,
        "parent_goal_id": str(task.goal_id),
        "source_task_id": str(task.id),
        "source_job_id": str(source_job_id) if source_job_id else None,
        "repository_identity": scope.repository_identity,
        "allowed_paths": list(allowed_paths),
        "required_capabilities": list(required_capabilities),
        "requested_capability": requested_capability,
        "expected_contribution": f"remediate {gap_type} for task {task.id}",
        "independent": True,
        "risk_level": getattr(task.risk_level, "value", str(task.risk_level)),
        "generation_depth": generation_depth,
        "approval_policy": "inherit_goal",
        "auto_approve": False,
        "failure_evidence": dict(failure_evidence or {}),
        "verification_requirements": list(task.verification_plan or []),
        "repair_recipe": repair_recipe,
        "reverify": dict(reverify) if reverify else None,
    }


def _live_generation_depth(db: Session, *, owner_id: uuid.UUID, task: MainAITask) -> int:
    """Derive lineage depth for live gap chaining. Ordinary (non-gap-generated) tasks are
    depth 0. A task whose created event claims `autonomous_gap_child:` lineage MUST resolve to
    an owner-scoped parent LifeProblem -- otherwise fail closed (GapLineageError), never return
    0 and silently unbound the depth limit."""
    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.owner_id == owner_id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return 0
    key = (event.detail or {}).get("insertion_idempotency_key", "")
    if not isinstance(key, str) or not key.startswith(GAP_CHILD_INSERTION_PREFIX):
        return 0
    parent_key = key[len(GAP_CHILD_INSERTION_PREFIX) :]
    parents = (
        db.execute(
            select(LifeProblem).where(
                LifeProblem.owner_id == owner_id,
                LifeProblem.idempotency_key == parent_key,
            )
        )
        .scalars()
        .all()
    )
    if len(parents) != 1:
        raise GapLineageError(
            f"gap child lineage for task {task.id} is unproven or ambiguous "
            f"(owner-scoped parent problems matching '{parent_key}': {len(parents)})"
        )
    provenance = parents[0].provenance or {}
    if "generation_depth" not in provenance:
        raise GapLineageError(
            f"parent LifeProblem for task {task.id} is missing provenance generation_depth"
        )
    return int(provenance["generation_depth"]) + 1


def gap_problem_for_child_task(db: Session, *, owner_id: uuid.UUID, task: MainAITask) -> LifeProblem | None:
    """Resolve the LifeProblem that produced a gap-generated child task, owner-scoped."""
    event = db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.owner_id == owner_id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return None
    key = (event.detail or {}).get("insertion_idempotency_key", "")
    if not isinstance(key, str) or not key.startswith(GAP_CHILD_INSERTION_PREFIX):
        return None
    return db.execute(
        select(LifeProblem).where(
            LifeProblem.owner_id == owner_id,
            LifeProblem.idempotency_key == key[len(GAP_CHILD_INSERTION_PREFIX) :],
        )
    ).scalar_one_or_none()


def is_gap_generated_child(db: Session, *, owner_id: uuid.UUID, task: MainAITask) -> bool:
    return gap_problem_for_child_task(db, owner_id=owner_id, task=task) is not None


def derive_work_binding_for_gap_child(
    db: Session,
    *,
    scope: SupervisorScope,
    task: MainAITask,
    prepare_context,
) -> WorkBinding | None:
    """Derive a WorkBinding for an inserted gap child from structured LifeProblem provenance.
    `candidate` is intentionally None -- Safe Planner / Provider-Assisted Planning run behind
    existing authority gates. Never auto-approves."""
    from app.development_supervisor.service import WorkBinding

    problem = gap_problem_for_child_task(db, owner_id=scope.owner_id, task=task)
    if problem is None:
        return None
    provenance = problem.provenance or {}
    envelope = dict(provenance.get("execution_envelope") or {})
    allowed_paths = tuple(envelope.get("allowed_paths") or provenance.get("allowed_paths") or scope.allowed_paths)
    allowed_paths = _intersect_paths(allowed_paths, tuple(scope.allowed_paths))
    if not allowed_paths:
        return None
    repository_identity = envelope.get("repository_identity") or provenance.get("repository_identity") or scope.repository_identity
    if repository_identity != scope.repository_identity:
        return None
    required_capabilities = tuple(
        envelope.get("required_capabilities")
        or provenance.get("required_capabilities")
        or ()
    )
    return WorkBinding(
        task_id=task.id,
        prepare_context=prepare_context,
        candidate=None,
        required_capabilities=required_capabilities,
        expected_contribution=envelope.get("expected_contribution") or task.description,
        independent=bool(envelope.get("independent", True)),
        repository_identity=repository_identity,
        allowed_paths=allowed_paths,
        allow_deterministic_fallback=True,
    )


def park_source_task_for_repair(db: Session, *, task: MainAITask, repair_child_id: uuid.UUID, reason: str) -> None:
    """Park the original failing task as blocked so a repair child can complete first, then the
    source can be resumed/re-verified. Does not finalize the task as permanently failed."""
    if task.status in {MainAITaskStatus.completed, MainAITaskStatus.cancelled, MainAITaskStatus.failed}:
        return
    task.status = MainAITaskStatus.blocked
    task.blocker_reason = f"{reason}; waiting on repair child {repair_child_id}"
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=task.owner_id,
            event_type=MainAITaskEventType.blocked,
            detail={
                "reason": "autonomous_gap_repair_park",
                "repair_child_id": str(repair_child_id),
            },
        )
    )
    db.flush()


def resume_source_after_repair(db: Session, *, owner_id: uuid.UUID, repair_child: MainAITask) -> MainAITask | None:
    """After a repair child completes, return the original source task to ready for re-verify."""
    problem = gap_problem_for_child_task(db, owner_id=owner_id, task=repair_child)
    if problem is None or problem.mainai_task_id is None:
        return None
    source = db.execute(
        select(MainAITask).where(
            MainAITask.id == problem.mainai_task_id,
            MainAITask.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if source is None:
        return None
    if source.status not in {
        MainAITaskStatus.blocked,
        MainAITaskStatus.retryable_failed,
        MainAITaskStatus.pending,
        MainAITaskStatus.running,
    }:
        return None
    source.status = MainAITaskStatus.ready
    source.blocker_reason = None
    source.next_retry_at = None
    db.add(
        MainAITaskEvent(
            task_id=source.id,
            owner_id=source.owner_id,
            event_type=MainAITaskEventType.retry_scheduled,
            detail={
                "reason": "autonomous_gap_repair_complete",
                "repair_child_id": str(repair_child.id),
            },
        )
    )
    db.flush()
    return source


def _repairable_failure_evidence(classification: str, detail: dict | None) -> dict | None:
    """Only create a repair child when structured evidence proves a bounded repair is needed."""
    detail = detail or {}
    if classification == "VERIFICATION_REQUIRED":
        return {
            "signal": classification,
            "verification_passed": False,
            "failed_capability": detail.get("capability") or detail.get("failed_capability"),
            "operator_result": detail.get("result"),
            "trace_event_id": detail.get("trace_event_id"),
            "requested_paths": detail.get("paths") or detail.get("path"),
            "step_arguments": dict(detail.get("step_arguments") or {}),
        }
    if classification == "FAILED_NONRETRYABLE":
        failed_capability = detail.get("capability") or detail.get("failed_capability")
        # Bounded repair only for verification/test/write failures inside the operator envelope --
        # not for arbitrary administrative failures.
        repairable = failed_capability in {
            "run_focused_test",
            "run_static_check",
            "patch_file",
            "create_file",
            "verification_evaluate",
        } or detail.get("verification_required") is True
        if not repairable and not detail:
            # Empty detail still may be a structured driver failure of a verification step when
            # the classification itself is FAILED_NONRETRYABLE after verification-shaped work;
            # require at least one concrete field to avoid inventing repairs from ambient noise.
            return None
        if not repairable:
            return None
        return {
            "signal": classification,
            "failed_capability": failed_capability,
            "operator_result": detail.get("result"),
            "trace_event_id": detail.get("trace_event_id"),
            "verification_required": bool(detail.get("verification_required")),
            "step_arguments": dict(detail.get("step_arguments") or {}),
            "paths": detail.get("paths") or detail.get("path"),
        }
    return None


def gap_from_verification_required(
    *,
    db: Session,
    scope: SupervisorScope,
    goal: MainAIGoal,
    task: MainAITask,
    binding_paths: tuple[str, ...] | None = None,
    source_job_id: uuid.UUID | None = None,
    failure_evidence: dict | None = None,
    operator_context: OperatorContext | None = None,
) -> DiscoveredGap:
    """Live verification/repair gap from structured driver evidence. Path scope is the narrowest
    intersection of SupervisorScope and the failing WorkBinding -- never a broader copy of the
    full scope when the execution binding was narrower."""
    depth = _live_generation_depth(db, owner_id=goal.owner_id, task=task)
    allowed_paths = _narrow_allowed_paths(scope=scope, binding_paths=binding_paths)
    if not allowed_paths:
        raise GapEvidenceError("failing WorkBinding path scope intersects authorized scope to empty set")
    evidence = dict(failure_evidence or {})
    evidence.setdefault("signal", "VERIFICATION_REQUIRED")
    repair_recipe = _structured_repair_recipe_for_gap(
        goal=goal,
        failure_evidence=evidence,
        allowed_paths=allowed_paths,
    )
    reverify = _reverify_contract_from_evidence(task=task, failure_evidence=evidence)
    envelope = _build_execution_envelope(
        scope=scope,
        task=task,
        allowed_paths=allowed_paths,
        gap_type="verification_failure",
        required_capabilities=(
            "create_file",
            "patch_file",
            "run_focused_test",
            "stage_scoped_changes",
            "commit_scoped_changes",
        ),
        failure_evidence=evidence,
        source_job_id=source_job_id,
        generation_depth=depth,
        repair_recipe=repair_recipe,
        reverify=reverify,
    )
    return DiscoveredGap(
        gap_type="verification_failure",
        evidence_kind="verification_failure",
        parent_goal_id=goal.id,
        reason=f"Task {task.id} ('{task.description}') did not reach verified completion; bounded repair required.",
        required_outcome=f"Repair the verification failure blocking: {task.description}",
        task_type="repo_edit",
        source_task_id=task.id,
        source_job_id=source_job_id,
        repository_identity=scope.repository_identity,
        allowed_paths=allowed_paths,
        required_capabilities=tuple(envelope["required_capabilities"]),
        risk_level=task.risk_level,
        verification_plan=tuple(task.verification_plan or ()),
        source_type="mainai_supervisor_verification_required",
        source_ref=f"mainai_task:{task.id}:verification_required",
        generation_depth=depth,
        execution_envelope=envelope,
        failure_evidence=evidence,
    )


def gap_from_capability_missing(
    *,
    db: Session,
    scope: SupervisorScope,
    goal: MainAIGoal,
    task: MainAITask,
    capability: str,
    binding_paths: tuple[str, ...] | None = None,
    source_job_id: uuid.UUID | None = None,
    failure_evidence: dict | None = None,
) -> DiscoveredGap:
    """Live capability gap. Capability name is mandatory -- never collapses to 'unknown'."""
    if not capability or not str(capability).strip() or str(capability).strip().lower() == "unknown":
        raise GapCapabilityError(
            "CAPABILITY_MISSING requires a concrete requested capability name; refusing to collapse to 'unknown'"
        )
    capability = str(capability).strip()
    depth = _live_generation_depth(db, owner_id=goal.owner_id, task=task)
    allowed_paths = _narrow_allowed_paths(scope=scope, binding_paths=binding_paths)
    if not allowed_paths:
        raise GapEvidenceError("failing WorkBinding path scope intersects authorized scope to empty set")
    evidence = dict(failure_evidence or {})
    evidence["requested_capability"] = capability
    envelope = _build_execution_envelope(
        scope=scope,
        task=task,
        allowed_paths=allowed_paths,
        gap_type="capability_missing",
        requested_capability=capability,
        required_capabilities=("create_file", "patch_file", "run_focused_test"),
        failure_evidence=evidence,
        source_job_id=source_job_id,
        generation_depth=depth,
    )
    return DiscoveredGap(
        gap_type="capability_missing",
        evidence_kind="capability_missing",
        parent_goal_id=goal.id,
        reason=f"Execution of task {task.id} ('{task.description}') reported a missing capability: {capability}.",
        required_outcome=f"Add deterministic support for the missing capability: {capability}",
        task_type="repo_edit",
        source_task_id=task.id,
        source_job_id=source_job_id,
        repository_identity=scope.repository_identity,
        allowed_paths=allowed_paths,
        risk_level=MainAIGoalRiskLevel.medium,
        source_type="mainai_supervisor_capability_missing",
        source_ref=f"mainai_task:{task.id}:capability_missing:{capability}",
        generation_depth=depth,
        execution_envelope=envelope,
        failure_evidence=evidence,
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
    operator_context: OperatorContext | None = None,
    binding_paths: tuple[str, ...] | None = None,
    source_job_id: uuid.UUID | None = None,
    driver_detail: dict | None = None,
    bounds: GapGenerationBounds | None = None,
    gaps_recorded_this_run: int = 0,
    children_inserted_this_run: int = 0,
    unresolved_gaps_this_run: int = 0,
) -> GapOutcome | None:
    """Live-wiring entry point. Lease-fenced when `operator_context` is supplied. Enforces live
    GapGenerationBounds across resume/takeover counters passed by the Supervisor. Never collapses
    a missing capability name to 'unknown'. Returns None for non-gap classifications."""
    if classification not in LIVE_GAP_SIGNAL_CLASSIFICATIONS:
        return None

    bounds = bounds or GapGenerationBounds()
    if gaps_recorded_this_run >= bounds.max_gaps_per_run:
        return GapOutcome(
            "GAPS_BOUND_REACHED",
            None,
            None,
            f"live gap breadth bound reached ({bounds.max_gaps_per_run})",
        )
    if children_inserted_this_run >= bounds.max_children_per_run:
        return GapOutcome(
            "CHILDREN_BOUND_REACHED",
            None,
            None,
            f"live children bound reached ({bounds.max_children_per_run})",
        )
    if unresolved_gaps_this_run >= bounds.max_unresolved_gaps:
        return GapOutcome(
            "UNRESOLVED_BOUND_REACHED",
            None,
            None,
            f"live unresolved-gap bound reached ({bounds.max_unresolved_gaps})",
        )

    if operator_context is not None:
        # Unlocked preflight only — the durable mutation fence is inside generate_child_task_for_gap.
        require_live_gap_lease(db, context=operator_context, for_update=False)

    try:
        if classification == "CAPABILITY_MISSING":
            gap = gap_from_capability_missing(
                db=db,
                scope=scope,
                goal=goal,
                task=task,
                capability=capability or "",
                binding_paths=binding_paths,
                source_job_id=source_job_id,
                failure_evidence={"signal": classification, **(driver_detail or {})},
            )
        else:
            evidence = _repairable_failure_evidence(classification, driver_detail)
            if evidence is None and classification == "FAILED_NONRETRYABLE":
                return None
            gap = gap_from_verification_required(
                db=db,
                scope=scope,
                goal=goal,
                task=task,
                binding_paths=binding_paths,
                source_job_id=source_job_id,
                failure_evidence=evidence or {"signal": classification, **(driver_detail or {})},
                operator_context=operator_context,
            )
    except GapLineageError as exc:
        return GapOutcome("DEPTH_BOUND_REACHED", None, None, str(exc))
    except GapCapabilityError as exc:
        return GapOutcome("CAPABILITY_MISSING", None, None, str(exc))
    except GapEvidenceError as exc:
        return GapOutcome("OUT_OF_SCOPE", None, None, str(exc))

    outcome = generate_child_task_for_gap(
        db,
        scope=scope,
        goal=goal,
        plan=plan,
        gap=gap,
        requested_by=requested_by,
        max_generation_depth=bounds.max_generation_depth,
        operator_context=operator_context,
    )
    if (
        outcome.classification == "ACCEPTED"
        and outcome.inserted_tasks
        and gap.gap_type == "verification_failure"
        and gap.source_task_id is not None
    ):
        park_source_task_for_repair(
            db,
            task=task,
            repair_child_id=outcome.inserted_tasks[0].id,
            reason="verification/repair gap recorded",
        )
    return outcome
