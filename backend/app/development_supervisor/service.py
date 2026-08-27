"""Bounded scoped autonomy over canonical MainAI development execution."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from app.autonomous_gap.service import (
    GapGenerationBounds,
    GapGenerationError,
    GapLeaseLostError,
    derive_work_binding_for_gap_child,
    gap_problem_for_child_task,
    handle_live_gap_signal,
    is_gap_generated_child,
    resume_source_after_repair,
)
from app.development_driver.service import run_driver
from app.development_operator.service import DEVELOPMENT_CAPABILITIES, OperatorContext
from app.intelligence_governance import record_evidence
from app.life_intents.service import IntentError, evaluate_feasibility
from app.jobs.mainai_job_lease import JobLeaseLostError
from app.jobs.service import mark_failed_flush
from app.mainai_execution.approval import ApprovalRequiredError
from app.mainai_execution.executor import _lock_task, dispatch_ready_task
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAIGoalStatus,
    MainAIPlan,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus
from app.models.work_intelligence import WorkStrategyExecution
from app.provider_planning.service import PlanningAdapter, plan_with_provider
from app.safe_planner.service import (
    CandidateStep,
    CandidateValidationError,
    FounderPlanningRequest,
    PlanCandidate,
    PlanningResult,
    build_multiplication_repair_candidate,
    plan_founder_request,
)

logger = logging.getLogger("mainai.development_supervisor")

AUTHORIZED_KINDS = frozenset(
    {"founder_requirement", "founder_decision", "founder_correction", "authorized_goal"}
)
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
TERMINAL = frozenset(
    {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled}
)

# Structured deferred reason codes -- never classify by English substring matching alone.
DEFERRED_CAPABILITY_MISSING = "CAPABILITY_MISSING"
DEFERRED_VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
DEFERRED_PROVIDER_SPEND_NOT_AUTHORIZED = "PROVIDER_SPEND_NOT_AUTHORIZED"
DEFERRED_WAITING_PROVIDER = "WAITING_PROVIDER"
DEFERRED_GAP_GENERATION_ERROR = "GAP_GENERATION_ERROR"

DEFERRED_REASON_MESSAGES = {
    DEFERRED_CAPABILITY_MISSING: "capability missing; bounded capability child required or authorization pending",
    DEFERRED_VERIFICATION_REQUIRED: "verification failed or remains incomplete; bounded repair required",
    DEFERRED_PROVIDER_SPEND_NOT_AUTHORIZED: "provider-assisted planning is not authorized for this scope",
    DEFERRED_WAITING_PROVIDER: "provider planning is unavailable",
    DEFERRED_GAP_GENERATION_ERROR: "gap generation failed closed; unrelated work may continue",
}


class SupervisorError(RuntimeError):
    pass


def _intersect_path_envelopes(*path_sets: tuple[str, ...]) -> tuple[str, ...]:
    nonempty = [tuple(paths) for paths in path_sets if paths]
    if not nonempty:
        return ()
    current = set(nonempty[0])
    for paths in nonempty[1:]:
        current &= set(paths)
    return tuple(sorted(current))


def bind_execution_context(
    *,
    scope: SupervisorScope,
    binding: WorkBinding,
    context: OperatorContext,
) -> OperatorContext:
    """Fail-closed path authority: OperatorContext may never exceed WorkBinding or scope.

    effective_allowed_paths = intersection(scope, binding, context).
    Never silently broadens the binding. Empty intersection rejects execution.
    """
    binding_paths = tuple(binding.allowed_paths or ())
    if not binding_paths:
        raise SupervisorError("WorkBinding.allowed_paths is required for execution")
    effective = _intersect_path_envelopes(
        tuple(scope.allowed_paths),
        binding_paths,
        tuple(context.allowed_paths or scope.allowed_paths),
    )
    if not effective:
        raise SupervisorError(
            "effective path envelope is empty after intersecting SupervisorScope, "
            "WorkBinding, and OperatorContext allowed_paths"
        )
    # Reject any context path that escapes the binding (do not keep extras).
    if set(context.allowed_paths or ()) - set(binding_paths):
        # Narrow safely to the intersection rather than aborting ordinary prepare_context
        # helpers that hand a broader parent envelope — but never add paths beyond binding.
        pass
    if not set(effective).issubset(set(binding_paths)):
        raise SupervisorError("effective paths escape WorkBinding.allowed_paths")
    if not set(effective).issubset(set(scope.allowed_paths)):
        raise SupervisorError("effective paths escape SupervisorScope.allowed_paths")
    return replace(context, allowed_paths=effective)


def _reverify_candidate_for_source(*, source: MainAITask, problem) -> PlanCandidate:
    """Build a re-verify-only candidate from structured gap evidence — never invent calculator tests."""
    envelope = ((problem.provenance or {}).get("execution_envelope") or {}) if problem is not None else {}
    contract = envelope.get("reverify") if isinstance(envelope, dict) else None
    if not isinstance(contract, dict):
        raise SupervisorError(
            "unsupported re-verification: gap envelope lacks a structured reverify contract"
        )
    capability = contract.get("capability")
    if capability == "run_focused_test":
        arguments = contract.get("arguments") or []
        if not isinstance(arguments, list) or not arguments:
            raise SupervisorError(
                "unsupported re-verification: focused-test contract missing arguments"
            )
        profile = contract.get("profile_name") or "focused_pytest"
        return PlanCandidate(
            interpretation="re-verify original work after autonomous repair",
            requested_outcome="original verification passes on the repaired repository",
            rationale="run the structured verification contract from the gap envelope only",
            steps=(
                CandidateStep(
                    "verify",
                    "run structured focused verification",
                    "pytest pass",
                    "run_focused_test",
                    {
                        "profile_name": profile,
                        "arguments": [str(item) for item in arguments],
                    },
                    required_risk="LOCAL_EXECUTION",
                    verification_required=True,
                ),
                CandidateStep(
                    "gate",
                    "evaluate verification evidence",
                    "verification pass",
                    "verification_evaluate",
                    depends_on=("verify",),
                ),
            ),
        )
    if capability == "run_static_check":
        return PlanCandidate(
            interpretation="re-verify original work after autonomous repair",
            requested_outcome="original static verification passes",
            rationale="run the structured static-check contract from the gap envelope only",
            steps=(
                CandidateStep(
                    "verify",
                    "run structured static check",
                    "static pass",
                    "run_static_check",
                    {},
                    required_risk="LOCAL_EXECUTION",
                    verification_required=True,
                ),
                CandidateStep(
                    "gate",
                    "evaluate verification evidence",
                    "verification pass",
                    "verification_evaluate",
                    depends_on=("verify",),
                ),
            ),
        )
    raise SupervisorError(
        f"unsupported re-verification capability '{capability}'; refusing calculator-global fallback"
    )


@dataclass(frozen=True)
class SupervisorBounds:
    max_candidates: int = 20
    max_jobs: int = 3
    max_elapsed_seconds: int = 900
    max_context_members: int = 50


@dataclass(frozen=True)
class SupervisorScope:
    owner_id: uuid.UUID
    goal_id: uuid.UUID
    authority_kind: str
    authority_ref: str
    authorized_instruction_sha256: str
    repository_identity: str
    allowed_paths: tuple[str, ...]
    allowed_capabilities: tuple[str, ...]
    maximum_risk: str = "low"
    completion_criteria: tuple[str, ...] = ()
    life_intent_id: uuid.UUID | None = None
    self_work: bool = False
    # Distinct from approval_policy on the goal: approval_policy gates the MainAITask's own
    # write/commit, but a WorkBinding with no deterministic candidate falls through to
    # plan_with_provider() -- a real, billed external API call. That is a spend decision, not
    # a repo-write decision, and the founder authorizing a scope for autonomous local work does
    # not imply authorizing it to spend money calling a provider (founder P1 review finding).
    # Defaults closed; the founder must opt a scope into provider spend explicitly.
    provider_spend_authorized: bool = False


def _live_provider_spend_authorized(db, scope: SupervisorScope) -> bool:
    """Effect-time spend gate: tick-start boolean is not enough.

    `scope.provider_spend_authorized` is reconstructed at Supervisor tick start from a live
    grant read. A founder revoke/exhaust between that read and `plan_with_provider` must still
    fail closed here — reserve inside planning is the final fence; this re-read closes the
    Supervisor dispatch window the same way envelope reverify closes authority TOCTOU.
    """
    if not scope.provider_spend_authorized:
        return False
    from app.provider_spend import provider_spend_is_live

    envelope_id: uuid.UUID | None = None
    if scope.authority_kind == "authorized_goal":
        try:
            envelope_id = uuid.UUID(str(scope.authority_ref))
        except ValueError:
            return False
    return provider_spend_is_live(
        db,
        owner_id=scope.owner_id,
        goal_id=scope.goal_id,
        execution_envelope_id=envelope_id,
    )


@dataclass(frozen=True)
class WorkBinding:
    """Execution material for one existing MainAI task, never canonical backlog truth."""

    task_id: uuid.UUID
    prepare_context: Callable[[MainAITask, MainAIJob], object]
    candidate: PlanCandidate | None = None
    provider_adapter: PlanningAdapter | None = None
    required_capabilities: tuple[str, ...] = ()
    expected_contribution: str = "advance the authorized goal"
    independent: bool = True
    ambiguity_refs: tuple[str, ...] = ()
    contradiction_refs: tuple[str, ...] = ()
    provider_likely: bool = False
    repository_identity: str = ""
    allowed_paths: tuple[str, ...] = ()
    # Gap-derived children may use Safe Planner's deterministic recipe registry when
    # candidate is None. Ordinary caller-supplied bindings without a candidate still require
    # explicit provider_spend_authorized before any billed provider call -- they must not
    # silently pick up a registry recipe that happens to match the goal instruction.
    allow_deterministic_fallback: bool = False


@dataclass(frozen=True)
class CandidateAssessment:
    task_id: uuid.UUID
    purpose: str
    status: str
    actionable: bool
    reason: str
    priority: int
    source: str = "mainai_task"
    expected_contribution: str = ""
    deferred_code: str | None = None


@dataclass(frozen=True)
class SupervisorResult:
    classification: str
    goal_id: uuid.UUID
    completed_jobs: int
    selected_task_ids: tuple[uuid.UUID, ...]
    candidates: tuple[CandidateAssessment, ...]
    explanation: dict
    checkpoint_id: uuid.UUID | None = None


def instruction_sha256(instruction: str) -> str:
    return hashlib.sha256(instruction.encode()).hexdigest()


def _goal(db, scope, *, lock=False):
    query = select(MainAIGoal).where(
        MainAIGoal.id == scope.goal_id, MainAIGoal.owner_id == scope.owner_id
    )
    goal = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if goal is None:
        raise SupervisorError("parent goal is missing or belongs to another owner")
    return goal


def validate_scope(db, scope: SupervisorScope):
    goal = _goal(db, scope)
    if scope.authority_kind not in AUTHORIZED_KINDS or not scope.authority_ref.strip():
        return goal, "NEEDS_AUTHORIZATION", "explicit founder authority is required"
    if (
        instruction_sha256(goal.original_instruction)
        != scope.authorized_instruction_sha256
    ):
        return (
            goal,
            "AUTHORITY_CHANGED",
            "founder instruction changed or was superseded",
        )
    if scope.maximum_risk not in RISK_ORDER:
        raise SupervisorError("risk envelope is invalid")
    if not scope.repository_identity.strip() or not scope.allowed_paths:
        raise SupervisorError("bounded repository/path scope is required")
    allowed = set(scope.allowed_capabilities)
    if not allowed or allowed - set(DEVELOPMENT_CAPABILITIES):
        raise SupervisorError(
            "capability envelope contains missing or unsafe capability"
        )
    if goal.status in {MainAIGoalStatus.cancelled, MainAIGoalStatus.completed}:
        return (
            goal,
            "CANCELLED" if goal.status == MainAIGoalStatus.cancelled else "COMPLETE",
            "goal is terminal",
        )
    if scope.life_intent_id:
        try:
            feasibility = evaluate_feasibility(
                db, owner_id=scope.owner_id, intent_id=scope.life_intent_id
            )
        except IntentError as exc:
            raise SupervisorError(str(exc)) from exc
        if not feasibility.actionable:
            return goal, "BLOCKED", "linked Life Intent is not actionable"
    return goal, None, None


def discover_candidates(db, *, scope, bindings, bounds):
    if not 1 <= bounds.max_candidates <= 100:
        raise SupervisorError("candidate bound is invalid")
    tasks = (
        db.execute(
            select(MainAITask)
            .where(
                MainAITask.owner_id == scope.owner_id,
                MainAITask.goal_id == scope.goal_id,
            )
            .order_by(
                MainAITask.priority.desc(),
                MainAITask.created_at.asc(),
                MainAITask.id.asc(),
            )
            .limit(bounds.max_candidates + 1)
        )
        .scalars()
        .all()
    )
    if len(tasks) > bounds.max_candidates:
        raise SupervisorError("candidate discovery bound reached")
    binding_map = {binding.task_id: binding for binding in bindings}
    assessments = []
    for task in tasks:
        binding = binding_map.get(task.id)
        reason = "canonical task is ready"
        actionable = task.status in {MainAITaskStatus.ready, MainAITaskStatus.running}
        if task.status in TERMINAL:
            reason, actionable = f"task is {task.status.value}", False
        elif task.status in {
            MainAITaskStatus.waiting_external,
            MainAITaskStatus.waiting_ci,
        }:
            reason, actionable = f"task is {task.status.value}", False
        elif task.status in {MainAITaskStatus.blocked, MainAITaskStatus.pending}:
            reason, actionable = (
                task.blocker_reason or f"task is {task.status.value}",
                False,
            )
        elif binding is None:
            reason, actionable = "execution binding is unavailable", False
        elif binding.repository_identity != scope.repository_identity or not set(
            binding.allowed_paths
        ).issubset(set(scope.allowed_paths)):
            reason, actionable = "out_of_scope repository or path binding", False
        elif (
            RISK_ORDER[getattr(task.risk_level, "value", task.risk_level)]
            > RISK_ORDER[scope.maximum_risk]
        ):
            reason, actionable = "task exceeds authorized risk envelope", False
        elif set(binding.required_capabilities) - set(scope.allowed_capabilities):
            reason, actionable = (
                "required capability is outside authorized envelope",
                False,
            )
        elif binding.ambiguity_refs:
            reason, actionable = "material ambiguity remains", False
        elif binding.contradiction_refs:
            reason, actionable = "founder authority contradiction remains", False
        assessments.append(
            CandidateAssessment(
                task.id,
                task.description,
                task.status.value,
                actionable,
                reason,
                task.priority,
                expected_contribution=(
                    binding.expected_contribution if binding else ""
                ),
            )
        )
    return tuple(assessments)


def select_candidate(assessments):
    actionable = [item for item in assessments if item.actionable]
    if not actionable:
        codes = {item.deferred_code for item in assessments if item.deferred_code}
        if "out_of_scope" in " ".join(item.reason for item in assessments) or any(
            "out_of_scope" in item.reason for item in assessments
        ):
            return (
                None,
                "OUT_OF_SCOPE",
                "candidate repository/path scope is unauthorized",
            )
        if DEFERRED_CAPABILITY_MISSING in codes:
            return (
                None,
                "CAPABILITY_MISSING",
                "a bounded candidate has a durable capability gap",
            )
        if DEFERRED_VERIFICATION_REQUIRED in codes:
            return (
                None,
                "VERIFICATION_REQUIRED",
                "a bounded candidate requires verification repair",
            )
        if DEFERRED_PROVIDER_SPEND_NOT_AUTHORIZED in codes:
            return (
                None,
                "PROVIDER_SPEND_NOT_AUTHORIZED",
                "provider-assisted planning is not authorized for this scope",
            )
        if DEFERRED_WAITING_PROVIDER in codes:
            return None, "WAITING_PROVIDER", "provider-dependent work is waiting"
        if DEFERRED_GAP_GENERATION_ERROR in codes:
            return None, "BLOCKED", "gap generation failed closed with no other actionable work"
        return None, "BLOCKED", "no bounded candidate is currently actionable"
    highest = max(item.priority for item in actionable)
    tied = [item for item in actionable if item.priority == highest]
    if len(tied) > 1:
        return (
            None,
            "NEEDS_SELECTION",
            "materially tied tasks have equal canonical priority",
        )
    return tied[0], "SELECTED", "highest-priority actionable canonical task"


def _completed_repair_child_for_source(
    db, *, owner_id: uuid.UUID, source: MainAITask
) -> tuple[object, MainAITask] | None:
    """Find a completed gap repair child whose LifeProblem points at `source`.

    Used to rebuild a durable re-verify PlanCandidate on a later tick after process death —
    the same-run path already mutates in-memory bindings, but production_entry rebuilds
    plain bindings every tick and would otherwise lose the reverify contract."""
    siblings = (
        db.execute(
            select(MainAITask).where(
                MainAITask.owner_id == owner_id,
                MainAITask.goal_id == source.goal_id,
                MainAITask.id != source.id,
                MainAITask.status == MainAITaskStatus.completed,
            )
        )
        .scalars()
        .all()
    )
    for child in siblings:
        problem = gap_problem_for_child_task(db, owner_id=owner_id, task=child)
        if problem is None or problem.mainai_task_id != source.id:
            continue
        envelope = ((problem.provenance or {}).get("execution_envelope") or {})
        if isinstance(envelope.get("reverify"), dict):
            return problem, child
    return None


def _derive_reverify_binding_for_repaired_source(
    db, *, scope, task: MainAITask, prepare_context, existing: WorkBinding
) -> WorkBinding | None:
    """Rebuild re-verify material from durable gap envelope when production rebuilt a plain binding."""
    if existing.candidate is not None or existing.allow_deterministic_fallback:
        return None
    if is_gap_generated_child(db, owner_id=scope.owner_id, task=task):
        return None
    if task.status not in {MainAITaskStatus.ready, MainAITaskStatus.running}:
        return None
    found = _completed_repair_child_for_source(db, owner_id=scope.owner_id, source=task)
    if found is None:
        return None
    problem, _child = found
    try:
        candidate = _reverify_candidate_for_source(source=task, problem=problem)
    except SupervisorError:
        return None
    envelope = dict(((problem.provenance or {}).get("execution_envelope") or {}))
    allowed_paths = tuple(envelope.get("allowed_paths") or existing.allowed_paths or scope.allowed_paths)
    allowed_paths = tuple(p for p in allowed_paths if p in set(scope.allowed_paths))
    if not allowed_paths:
        return None
    required_capabilities = tuple(
        envelope.get("required_capabilities")
        or ("run_focused_test", "run_static_check")
    )
    return WorkBinding(
        task_id=task.id,
        prepare_context=prepare_context,
        candidate=candidate,
        required_capabilities=required_capabilities,
        expected_contribution="re-verify original work after repair",
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=allowed_paths,
        allow_deterministic_fallback=False,
    )


def _augment_bindings_with_gap_children(db, *, scope, bindings: tuple[WorkBinding, ...]) -> tuple[WorkBinding, ...]:
    """Close the live handoff: discover gap-generated children and derive WorkBindings from
    structured gap evidence. Does not invent PlanCandidates or auto-approve.

    Production ticks (`production_entry`) rebuild plain WorkBindings for every ready/running
    task each invocation. Those plains MUST NOT permanently shadow a durable gap child or a
    post-repair re-verify source: replace (do not skip) known bindings when durable gap
    evidence supplies a narrower, recipe-capable binding. Explicit caller candidates are kept.
    """
    if not bindings:
        return bindings
    prepare = bindings[0].prepare_context
    known = {binding.task_id: binding for binding in bindings}
    tasks = (
        db.execute(
            select(MainAITask).where(
                MainAITask.owner_id == scope.owner_id,
                MainAITask.goal_id == scope.goal_id,
                MainAITask.status.in_(
                    [
                        MainAITaskStatus.ready,
                        MainAITaskStatus.pending,
                        MainAITaskStatus.running,
                        MainAITaskStatus.blocked,
                    ]
                ),
            )
        )
        .scalars()
        .all()
    )
    replacements: dict[uuid.UUID, WorkBinding] = {}
    additions: list[WorkBinding] = []
    for task in tasks:
        existing = known.get(task.id)
        if is_gap_generated_child(db, owner_id=scope.owner_id, task=task):
            # Never let a production-plain pre-bind (candidate=None, no fallback) hide the
            # durable gap recipe/path envelope across ticks.
            if existing is not None and (
                existing.candidate is not None or existing.allow_deterministic_fallback
            ):
                continue
            derived = derive_work_binding_for_gap_child(
                db, scope=scope, task=task, prepare_context=prepare
            )
            if derived is None:
                continue
            if existing is None:
                additions.append(derived)
                known[task.id] = derived
            else:
                replacements[task.id] = derived
                known[task.id] = derived
            continue
        if existing is not None:
            reverify = _derive_reverify_binding_for_repaired_source(
                db,
                scope=scope,
                task=task,
                prepare_context=prepare,
                existing=existing,
            )
            if reverify is not None:
                replacements[task.id] = reverify
                known[task.id] = reverify
    if not replacements and not additions:
        return bindings
    rebuilt = tuple(replacements.get(binding.task_id, binding) for binding in bindings)
    return rebuilt + tuple(additions)


def _release_midflight_job_and_set_task_status(
    db,
    *,
    task: MainAITask,
    job: MainAIJob,
    context: OperatorContext,
    phase: str,
    task_status: MainAITaskStatus,
    event_reason: str,
    blocker_reason: str | None = None,
) -> None:
    """Fence-fail a supervisor mid-flight job and move the task off `running`.

    Used for durable provider waits so process death cannot route through ordinary
    `task_execution` recovery. `WAITING_PROVIDER` returns the task to `ready` (next tick
    wakes). `PROVIDER_SPEND_NOT_AUTHORIZED` parks as `blocked` (no redispatch spam until
    spend is founder-authorized / task is explicitly unblocked). Flush-only.
    """
    try:
        mark_failed_flush(
            db,
            job,
            worker_id=context.worker_id,
            lease_generation=context.lease_generation,
            error_category=MainAIJobErrorCategory.capability_unavailable,
        )
    except JobLeaseLostError:
        logger.warning(
            "midflight release lost job lease for task %s job %s phase=%s; leaving task state alone",
            task.id,
            job.id,
            phase,
        )
        return
    locked = _lock_task(db, task.id)
    if locked.status != MainAITaskStatus.running:
        return
    locked.status = task_status
    locked.next_retry_at = None
    locked.blocker_reason = blocker_reason
    event_type = (
        MainAITaskEventType.blocked
        if task_status == MainAITaskStatus.blocked
        else MainAITaskEventType.retry_scheduled
    )
    db.add(
        MainAITaskEvent(
            task_id=locked.id,
            owner_id=locked.owner_id,
            event_type=event_type,
            detail={
                "reason": event_reason,
                "phase": phase,
                "released_job_id": str(job.id),
                "attempts": locked.attempts,
            },
        )
    )
    db.flush()


def _release_provider_wait_midflight(
    db, *, task: MainAITask, job: MainAIJob, context: OperatorContext, phase: str
) -> None:
    """B7: WAITING_PROVIDER → fence-fail job + durable backoff park (not immediate ready).

    Immediate ready caused a hot loop while the provider stayed down. Park as `blocked` with
    `next_retry_at` so the worker clock wakes the task later; exhausted after
    WAITING_PROVIDER_MAX_BACKOFFS with no clock (not fabricated permanent incapability).
    """
    from app.mainai_execution.provider_wait_wake import (
        WAITING_PROVIDER_BACKOFF_REASON,
        WAITING_PROVIDER_BLOCKER,
        WAITING_PROVIDER_EXHAUSTED_REASON,
        compute_waiting_provider_retry_at,
        count_waiting_provider_backoffs,
    )

    prior = count_waiting_provider_backoffs(db, task=task)
    retry_at = compute_waiting_provider_retry_at(prior_backoffs=prior)
    exhausted = retry_at is None
    _release_midflight_job_and_set_task_status(
        db,
        task=task,
        job=job,
        context=context,
        phase=phase,
        task_status=MainAITaskStatus.blocked,
        event_reason=WAITING_PROVIDER_EXHAUSTED_REASON if exhausted else WAITING_PROVIDER_BACKOFF_REASON,
        blocker_reason=WAITING_PROVIDER_BLOCKER,
    )
    locked = _lock_task(db, task.id)
    if locked.status == MainAITaskStatus.blocked:
        locked.next_retry_at = retry_at
        db.flush()


def _park_provider_spend_defer_midflight(
    db, *, task: MainAITask, job: MainAIJob, context: OperatorContext
) -> None:
    """Spend denial → fence-fail job + park task blocked (no ready-loop spam)."""
    from app.mainai_execution.provider_wait_wake import (
        PROVIDER_SPEND_PARK_BLOCKER,
        PROVIDER_SPEND_PARK_REASON,
    )

    _release_midflight_job_and_set_task_status(
        db,
        task=task,
        job=job,
        context=context,
        phase="PROVIDER_SPEND_NOT_AUTHORIZED",
        task_status=MainAITaskStatus.blocked,
        event_reason=PROVIDER_SPEND_PARK_REASON,
        blocker_reason=PROVIDER_SPEND_PARK_BLOCKER,
    )


def _apply_deferred(assessments, deferred: dict[uuid.UUID, str]):
    return tuple(
        replace(
            item,
            actionable=False,
            deferred_code=deferred[item.task_id],
            reason=DEFERRED_REASON_MESSAGES.get(
                deferred[item.task_id], deferred[item.task_id]
            ),
        )
        if item.task_id in deferred
        else item
        for item in assessments
    )


def _checkpoint(db, *, goal, task, job_id, phase, state):
    row = MainAICheckpoint(
        goal_id=goal.id,
        task_id=task.id if task else None,
        owner_id=goal.owner_id,
        plan_version=goal.current_plan_version,
        executor_state={
            "job_id": str(job_id) if job_id else None,
            "step": "development_supervisor",
            "phase": phase,
            "supervisor_state": state,
        },
    )
    db.add(row)
    db.flush()
    return row


def _latest_state(db, scope):
    rows = (
        db.execute(
            select(MainAICheckpoint)
            .where(
                MainAICheckpoint.goal_id == scope.goal_id,
                MainAICheckpoint.owner_id == scope.owner_id,
            )
            .order_by(MainAICheckpoint.created_at.desc())
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.executor_state.get("step") == "development_supervisor":
            return row, row.executor_state.get("supervisor_state", {})
    return None, {}


def _result(
    classification,
    goal,
    completed,
    selected,
    candidates,
    reason,
    checkpoint=None,
    **detail,
):
    return SupervisorResult(
        classification,
        goal.id,
        completed,
        tuple(selected),
        tuple(candidates),
        {"reason": reason, **detail},
        checkpoint.id if checkpoint else None,
    )


async def run_supervisor(
    db,
    *,
    scope: SupervisorScope,
    bindings: tuple[WorkBinding, ...],
    bounds: SupervisorBounds | None = None,
    worker_id: str = "development-supervisor",
    gap_bounds: GapGenerationBounds | None = None,
):
    """Select and execute bounded existing work; every effect remains below Safe Planner.

    Gap children inserted mid-run are auto-bound from structured LifeProblem provenance so the
    live loop does not require founder/test job translation of WorkBinding/PlanCandidate.
    Founder approval policy remains authoritative at dispatch.

    Breadth counters (`gaps_recorded_this_run`, `children_inserted_this_run`,
    `unresolved_gaps_this_run`) are **per Supervisor invocation**: each call to
    `run_supervisor` starts them at zero. Prior checkpoints may still record the counters for
    observability of that invocation, but they are not restored across calls. Resume of
    completed_task_ids remains checkpoint-based.
    """
    bounds = bounds or SupervisorBounds()
    gap_bounds = gap_bounds or GapGenerationBounds()
    if not 1 <= bounds.max_jobs <= 20 or bounds.max_elapsed_seconds < 1:
        raise SupervisorError("supervisor execution bounds are invalid")
    started = time.monotonic()
    goal, terminal, reason = validate_scope(db, scope)
    if terminal:
        return _result(terminal, goal, 0, (), (), reason)
    prior_cp, prior = _latest_state(db, scope)
    selected = [uuid.UUID(value) for value in prior.get("completed_task_ids", [])]
    completed_jobs = len(selected)
    jobs_this_run = 0
    bindings = _augment_bindings_with_gap_children(db, scope=scope, bindings=bindings)
    binding_map = {binding.task_id: binding for binding in bindings}
    last_candidates = ()
    deferred: dict[uuid.UUID, str] = {}
    # Per-invocation breadth bounds (not sticky across Supervisor calls).
    gaps_recorded_this_run = 0
    children_inserted_this_run = 0
    unresolved_gaps_this_run = 0

    def _gap_state_extra(gap_outcome):
        return {
            "gaps_recorded_this_run": gaps_recorded_this_run,
            "children_inserted_this_run": children_inserted_this_run,
            "unresolved_gaps_this_run": unresolved_gaps_this_run,
            "gap_generation": None
            if gap_outcome is None
            else {
                "classification": gap_outcome.classification,
                "problem_id": str(gap_outcome.problem.id) if gap_outcome.problem else None,
                "inserted_task_ids": (
                    [str(t.id) for t in gap_outcome.inserted_tasks]
                    if gap_outcome.inserted_tasks
                    else []
                ),
            },
        }

    def _invoke_live_gap(
        *,
        task,
        classification,
        capability,
        context,
        binding,
        driver_detail,
        plan,
    ):
        nonlocal gaps_recorded_this_run, children_inserted_this_run, unresolved_gaps_this_run, bindings, binding_map
        try:
            gap_outcome = handle_live_gap_signal(
                db,
                scope=scope,
                goal=goal,
                plan=plan,
                task=task,
                classification=classification,
                capability=capability,
                requested_by=worker_id,
                operator_context=context,
                binding_paths=binding.allowed_paths,
                source_job_id=context.job_id,
                driver_detail=driver_detail or {},
                bounds=gap_bounds,
                gaps_recorded_this_run=gaps_recorded_this_run,
                children_inserted_this_run=children_inserted_this_run,
                unresolved_gaps_this_run=unresolved_gaps_this_run,
            )
        except GapLeaseLostError as exc:
            logger.warning("gap generation lease fence tripped: %s", exc)
            return GapLeaseLostError.__name__, None
        except GapGenerationError as exc:
            logger.warning("gap generation failed closed without stopping run: %s", exc)
            return DEFERRED_GAP_GENERATION_ERROR, None
        except Exception as exc:  # noqa: BLE001 -- isolate unrelated feasible work
            logger.exception("unexpected gap generation error isolated: %s", exc)
            return DEFERRED_GAP_GENERATION_ERROR, None

        if gap_outcome is None:
            return None, None
        gaps_recorded_this_run += 1
        if gap_outcome.classification == "ACCEPTED" and gap_outcome.inserted_tasks:
            children_inserted_this_run += 1
            bindings = _augment_bindings_with_gap_children(db, scope=scope, bindings=bindings)
            binding_map = {binding.task_id: binding for binding in bindings}
        elif gap_outcome.classification != "ACCEPTED":
            unresolved_gaps_this_run += 1
        return None, gap_outcome

    while (
        jobs_this_run < bounds.max_jobs
        and time.monotonic() - started < bounds.max_elapsed_seconds
    ):
        goal, terminal, reason = validate_scope(db, scope)
        if terminal:
            return _result(
                terminal,
                goal,
                completed_jobs,
                selected,
                last_candidates,
                reason,
                prior_cp,
            )
        bindings = _augment_bindings_with_gap_children(db, scope=scope, bindings=bindings)
        binding_map = {binding.task_id: binding for binding in bindings}
        assessments = discover_candidates(
            db, scope=scope, bindings=bindings, bounds=bounds
        )
        assessments = _apply_deferred(assessments, deferred)
        last_candidates = assessments
        choice, classification, selection_reason = select_candidate(assessments)
        if choice is None:
            incomplete = [
                item
                for item in assessments
                if item.status not in {"completed", "cancelled"}
            ]
            if not incomplete:
                if not scope.completion_criteria:
                    classification = "NEEDS_CLARIFICATION"
                    selection_reason = "parent completion criteria are not explicit"
                else:
                    goal = _goal(db, scope, lock=True)
                    goal.status = MainAIGoalStatus.completed
                    goal.completed_at = datetime.utcnow()
                    goal.final_outcome = "; ".join(scope.completion_criteria)
                    cp = _checkpoint(
                        db,
                        goal=goal,
                        task=None,
                        job_id=None,
                        phase="COMPLETE",
                        state={
                            "completed_task_ids": [str(value) for value in selected]
                        },
                    )
                    return _result(
                        "COMPLETE",
                        goal,
                        completed_jobs,
                        selected,
                        assessments,
                        "all explicit completion criteria have verified child work",
                        cp,
                    )
            cp = _checkpoint(
                db,
                goal=goal,
                task=None,
                job_id=None,
                phase=classification,
                state={
                    "completed_task_ids": [str(value) for value in selected],
                    "candidate_task_ids": [str(item.task_id) for item in assessments],
                    "reason": selection_reason,
                    "gaps_recorded_this_run": gaps_recorded_this_run,
                    "children_inserted_this_run": children_inserted_this_run,
                    "unresolved_gaps_this_run": unresolved_gaps_this_run,
                },
            )
            return _result(
                classification,
                goal,
                completed_jobs,
                selected,
                assessments,
                selection_reason,
                cp,
                followup="verification_repair"
                if classification == "VERIFICATION_REQUIRED"
                else None,
            )
        task = db.execute(
            select(MainAITask).where(
                MainAITask.id == choice.task_id,
                MainAITask.owner_id == scope.owner_id,
                MainAITask.goal_id == scope.goal_id,
            )
        ).scalar_one()
        binding = binding_map[task.id]
        try:
            if task.status == MainAITaskStatus.ready:
                job = dispatch_ready_task(
                    db, task=task, goal=goal, dispatched_by=worker_id
                )
            else:
                job = db.execute(
                    select(MainAIJob).where(
                        MainAIJob.id == task.mainai_job_id,
                        MainAIJob.owner_id == scope.owner_id,
                    )
                ).scalar_one()
        except ApprovalRequiredError:
            cp = _checkpoint(
                db,
                goal=goal,
                task=task,
                job_id=None,
                phase="WAITING_APPROVAL",
                state={
                    "completed_task_ids": [str(value) for value in selected],
                    "selected_task_id": str(task.id),
                    **_gap_state_extra(None),
                },
            )
            return _result(
                "WAITING_APPROVAL",
                goal,
                completed_jobs,
                selected,
                assessments,
                "selected task requires founder approval",
                cp,
            )
        context = binding.prepare_context(task, job)
        if (
            context.owner_id != scope.owner_id
            or context.task_id != task.id
            or context.job_id != job.id
            or str(context.repository_root.resolve()) != scope.repository_identity
        ):
            raise SupervisorError(
                "execution context escapes authorized owner/repository scope"
            )
        context = bind_execution_context(scope=scope, binding=binding, context=context)
        strategy_execution = db.execute(
            select(WorkStrategyExecution).where(
                WorkStrategyExecution.id == context.strategy_execution_id,
                WorkStrategyExecution.owner_id == scope.owner_id,
            )
        ).scalar_one()
        record_evidence(
            db,
            owner_id=scope.owner_id,
            execution_id=strategy_execution.execution_id,
            evidence_kind="supervisor_selection",
            payload={
                "candidate_task_ids": [str(item.task_id) for item in assessments],
                "selected_task_id": str(task.id),
                "selection_reason": selection_reason,
                "expected_contribution": binding.expected_contribution,
            },
            source_type="mainai_goal",
            source_ref=str(goal.id),
            idempotency_key=f"supervisor-select:{task.id}",
            review_kind="deterministic_tool",
            deterministic=True,
        )
        request = FounderPlanningRequest(
            owner_id=scope.owner_id,
            goal_id=goal.id,
            task_id=task.id,
            job_id=job.id,
            original_instruction=goal.original_instruction,
            authority_kind=scope.authority_kind,
            source_ref=scope.authority_ref,
            requested_outcome=task.description,
            repository_identity=scope.repository_identity,
            ambiguity_refs=binding.ambiguity_refs,
            contradiction_refs=binding.contradiction_refs,
            max_context_members=bounds.max_context_members,
        )
        # Explicit candidate always goes through Safe Planner validation.
        # Gap-derived bindings may use deterministic Safe Planner recipes (no provider spend).
        # Ordinary candidate=None bindings still require provider_spend_authorized before any
        # billed provider call -- they must not silently inherit a matching registry recipe.
        if binding.candidate is not None:
            planning = plan_founder_request(
                db,
                request=request,
                operator_context=context,
                candidate=binding.candidate,
            )
        elif binding.allow_deterministic_fallback:
            recipe_candidate = None
            problem = gap_problem_for_child_task(
                db, owner_id=scope.owner_id, task=task
            )
            recipe = (
                ((problem.provenance or {}).get("execution_envelope") or {}).get(
                    "repair_recipe"
                )
                if problem is not None
                else None
            )
            try:
                if recipe == "multiplication_repair":
                    recipe_candidate = build_multiplication_repair_candidate(context)
                planning = plan_founder_request(
                    db,
                    request=request,
                    operator_context=context,
                    candidate=recipe_candidate,
                )
            except CandidateValidationError as exc:
                planning = PlanningResult(
                    "OUT_OF_SCOPE",
                    {
                        "reason": str(exc),
                        "repair_recipe": recipe,
                    },
                )
            if planning.classification == "WAITING_PROVIDER":
                if not _live_provider_spend_authorized(db, scope):
                    cp = _checkpoint(
                        db,
                        goal=goal,
                        task=task,
                        job_id=job.id,
                        phase="PROVIDER_SPEND_NOT_AUTHORIZED",
                        state={
                            "completed_task_ids": [str(value) for value in selected],
                            "selected_task_id": str(task.id),
                        },
                    )
                    if binding.independent:
                        _park_provider_spend_defer_midflight(
                            db, task=task, job=job, context=context
                        )
                        deferred[task.id] = DEFERRED_PROVIDER_SPEND_NOT_AUTHORIZED
                        continue
                    _park_provider_spend_defer_midflight(
                        db, task=task, job=job, context=context
                    )
                    return _result(
                        "PROVIDER_SPEND_NOT_AUTHORIZED",
                        goal,
                        completed_jobs,
                        selected,
                        assessments,
                        "provider-assisted planning requires a still-live founder spend grant",
                        cp,
                    )
                planning = await plan_with_provider(
                    db,
                    request=request,
                    operator_context=context,
                    adapter=binding.provider_adapter,
                )
        elif not _live_provider_spend_authorized(db, scope):
            cp = _checkpoint(
                db,
                goal=goal,
                task=task,
                job_id=job.id,
                phase="PROVIDER_SPEND_NOT_AUTHORIZED",
                state={
                    "completed_task_ids": [str(value) for value in selected],
                    "selected_task_id": str(task.id),
                },
            )
            if binding.independent:
                _park_provider_spend_defer_midflight(
                    db, task=task, job=job, context=context
                )
                deferred[task.id] = DEFERRED_PROVIDER_SPEND_NOT_AUTHORIZED
                continue
            _park_provider_spend_defer_midflight(
                db, task=task, job=job, context=context
            )
            return _result(
                "PROVIDER_SPEND_NOT_AUTHORIZED",
                goal,
                completed_jobs,
                selected,
                assessments,
                "provider-assisted planning requires a still-live founder spend grant",
                cp,
            )
        else:
            planning = await plan_with_provider(
                db,
                request=request,
                operator_context=context,
                adapter=binding.provider_adapter,
            )
        if planning.classification != "ACCEPTED" or planning.plan is None:
            gap_outcome = None
            gap_error = None
            if planning.classification == "CAPABILITY_MISSING":
                gap_error, gap_outcome = _invoke_live_gap(
                    task=task,
                    classification="CAPABILITY_MISSING",
                    capability=(planning.explanation or {}).get("requested_capability"),
                    context=context,
                    binding=binding,
                    driver_detail=planning.explanation or {},
                    plan=db.get(MainAIPlan, task.plan_id),
                )
            cp = _checkpoint(
                db,
                goal=goal,
                task=task,
                job_id=job.id,
                phase=planning.classification,
                state={
                    "completed_task_ids": [str(value) for value in selected],
                    "selected_task_id": str(task.id),
                    "planning_checkpoint_id": str(planning.checkpoint_id)
                    if planning.checkpoint_id
                    else None,
                    **_gap_state_extra(gap_outcome),
                    "gap_generation_error": gap_error,
                },
            )
            if gap_error == GapLeaseLostError.__name__:
                deferred[task.id] = DEFERRED_GAP_GENERATION_ERROR
                continue
            if planning.classification == "WAITING_PROVIDER" and binding.independent:
                _release_provider_wait_midflight(
                    db, task=task, job=job, context=context, phase="WAITING_PROVIDER"
                )
                deferred[task.id] = DEFERRED_WAITING_PROVIDER
                continue
            if planning.classification == "CAPABILITY_MISSING" and binding.independent:
                deferred[task.id] = DEFERRED_CAPABILITY_MISSING
                continue
            if gap_error == DEFERRED_GAP_GENERATION_ERROR and binding.independent:
                deferred[task.id] = DEFERRED_GAP_GENERATION_ERROR
                continue
            if planning.classification == "WAITING_PROVIDER":
                _release_provider_wait_midflight(
                    db, task=task, job=job, context=context, phase="WAITING_PROVIDER"
                )
            return _result(
                planning.classification,
                goal,
                completed_jobs,
                selected,
                assessments,
                planning.explanation.get(
                    "reason", "planning did not produce an executable plan"
                ),
                cp,
            )
        # ACCEPTED → derive plan-cited paths/capabilities, intersect envelope, enforce at Driver.
        from app.development_driver.service import DRIVER_DIRECTIVES
        from app.development_supervisor.plan_scope_narrowing import (
            PlanScopeNarrowingError,
            narrow_task_scope_from_accepted_development_plan,
        )

        try:
            narrowed = narrow_task_scope_from_accepted_development_plan(
                envelope_paths=tuple(scope.allowed_paths),
                envelope_capabilities=tuple(scope.allowed_capabilities),
                plan=planning.plan,
            )
            operator_steps = [
                step
                for step in planning.plan.steps
                if getattr(step, "capability", None) not in DRIVER_DIRECTIVES
            ]
            if operator_steps:
                if not narrowed.allowed_paths:
                    raise PlanScopeNarrowingError(
                        "accepted plan cites no paths; refusing to fall back to full envelope"
                    )
                if not narrowed.allowed_capabilities:
                    raise PlanScopeNarrowingError(
                        "accepted plan cites no capabilities; refusing to fall back to full envelope"
                    )
                exec_context = replace(
                    context,
                    allowed_paths=narrowed.allowed_paths,
                    allowed_capabilities=narrowed.allowed_capabilities,
                )
                exec_context = bind_execution_context(
                    scope=scope, binding=binding, context=exec_context
                )
            else:
                # Directive-only plans (e.g. verification_evaluate) — Driver owns them; do not
                # invent path/capability ceilings from an empty citation set.
                exec_context = context
        except PlanScopeNarrowingError as exc:
            cp = _checkpoint(
                db,
                goal=goal,
                task=task,
                job_id=job.id,
                phase="OUT_OF_SCOPE",
                state={
                    "completed_task_ids": [str(value) for value in selected],
                    "selected_task_id": str(task.id),
                    "planning_checkpoint_id": str(planning.checkpoint_id)
                    if planning.checkpoint_id
                    else None,
                    "narrowing_error": str(exc),
                },
            )
            return _result(
                "OUT_OF_SCOPE",
                goal,
                completed_jobs,
                selected,
                assessments,
                str(exc),
                cp,
            )
        driver = run_driver(db, context=exec_context, plan=planning.plan)
        if driver.classification != "COMPLETE":
            if driver.classification in {
                "VERIFICATION_REQUIRED",
                "FAILED_NONRETRYABLE",
            }:
                task.blocker_reason = DEFERRED_REASON_MESSAGES[DEFERRED_VERIFICATION_REQUIRED]
            gap_error, gap_outcome = _invoke_live_gap(
                task=task,
                classification=driver.classification,
                capability=(
                    driver.detail.get("requested_capability")
                    if driver.classification == "CAPABILITY_MISSING"
                    else None
                ),
                context=context,
                binding=binding,
                driver_detail=driver.detail or {},
                plan=db.get(MainAIPlan, task.plan_id),
            )
            cp = _checkpoint(
                db,
                goal=goal,
                task=task,
                job_id=job.id,
                phase=driver.classification,
                state={
                    "completed_task_ids": [str(value) for value in selected],
                    "selected_task_id": str(task.id),
                    "driver_checkpoint_id": str(driver.checkpoint_id),
                    "followup": "verification_repair"
                    if driver.classification
                    in {"VERIFICATION_REQUIRED", "FAILED_NONRETRYABLE"}
                    else None,
                    **_gap_state_extra(gap_outcome),
                    "gap_generation_error": gap_error,
                },
            )
            deferable = driver.classification in {
                "CAPABILITY_MISSING",
                "VERIFICATION_REQUIRED",
                "FAILED_NONRETRYABLE",
            } or gap_error is not None
            if deferable and binding.independent:
                if gap_error == GapLeaseLostError.__name__ or gap_error == DEFERRED_GAP_GENERATION_ERROR:
                    deferred[task.id] = DEFERRED_GAP_GENERATION_ERROR
                elif driver.classification == "CAPABILITY_MISSING":
                    deferred[task.id] = DEFERRED_CAPABILITY_MISSING
                else:
                    deferred[task.id] = DEFERRED_VERIFICATION_REQUIRED
                continue
            return _result(
                driver.classification,
                goal,
                completed_jobs,
                selected,
                assessments,
                "child work did not reach verified completion",
                cp,
                followup="verification_repair"
                if driver.classification
                in {"VERIFICATION_REQUIRED", "FAILED_NONRETRYABLE"}
                else None,
            )
        selected.append(task.id)
        completed_jobs += 1
        jobs_this_run += 1
        if is_gap_generated_child(db, owner_id=scope.owner_id, task=task):
            resumed = resume_source_after_repair(
                db, owner_id=scope.owner_id, repair_child=task
            )
            if resumed is not None:
                deferred.pop(resumed.id, None)
                existing = binding_map.get(resumed.id)
                repair_problem = gap_problem_for_child_task(
                    db, owner_id=scope.owner_id, task=task
                )
                reverify = _reverify_candidate_for_source(
                    source=resumed, problem=repair_problem
                )
                envelope_paths = tuple(
                    ((repair_problem.provenance or {}).get("execution_envelope") or {}).get(
                        "allowed_paths"
                    )
                    or scope.allowed_paths
                ) if repair_problem is not None else tuple(scope.allowed_paths)
                if existing is not None:
                    updated = replace(
                        existing,
                        candidate=reverify,
                        allowed_paths=existing.allowed_paths or envelope_paths,
                    )
                    binding_map[resumed.id] = updated
                    bindings = tuple(
                        binding_map.get(item.task_id, item) for item in bindings
                    )
                else:
                    derived = WorkBinding(
                        task_id=resumed.id,
                        prepare_context=bindings[0].prepare_context,
                        candidate=reverify,
                        required_capabilities=("run_focused_test", "run_static_check"),
                        expected_contribution="re-verify original work after repair",
                        independent=True,
                        repository_identity=scope.repository_identity,
                        allowed_paths=envelope_paths or tuple(scope.allowed_paths),
                    )
                    bindings = bindings + (derived,)
                    binding_map[resumed.id] = derived
            bindings = _augment_bindings_with_gap_children(
                db, scope=scope, bindings=bindings
            )
            binding_map = {binding.task_id: binding for binding in bindings}
        prior_cp = _checkpoint(
            db,
            goal=goal,
            task=task,
            job_id=job.id,
            phase="REASSESS",
            state={
                "completed_task_ids": [str(value) for value in selected],
                "last_verified_task_id": str(task.id),
                "selection_reason": selection_reason,
                "gaps_recorded_this_run": gaps_recorded_this_run,
                "children_inserted_this_run": children_inserted_this_run,
                "unresolved_gaps_this_run": unresolved_gaps_this_run,
            },
        )
    return _result(
        "RUN_BOUND_REACHED",
        goal,
        completed_jobs,
        selected,
        last_candidates,
        "supervisor invocation bound reached; state is resumable",
        prior_cp,
    )


def record_founder_correction(db, *, scope, corrected_instruction):
    """Explicit helper for tests/callers; correction changes canonical authority, never silently."""
    goal = _goal(db, scope, lock=True)
    goal.original_instruction = corrected_instruction
    goal.status = MainAIGoalStatus.running
    db.flush()
    return goal
