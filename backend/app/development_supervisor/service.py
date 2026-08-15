"""Bounded scoped autonomy over canonical MainAI development execution."""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from app.development_driver.service import run_driver
from app.development_operator.service import DEVELOPMENT_CAPABILITIES
from app.intelligence_governance import record_evidence
from app.life_intents.service import IntentError, evaluate_feasibility
from app.mainai_execution.approval import ApprovalRequiredError
from app.mainai_execution.executor import dispatch_ready_task
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob
from app.models.work_intelligence import WorkStrategyExecution
from app.provider_planning.service import PlanningAdapter, plan_with_provider
from app.safe_planner.service import (
    FounderPlanningRequest,
    PlanCandidate,
    plan_founder_request,
)


AUTHORIZED_KINDS = frozenset(
    {"founder_requirement", "founder_decision", "founder_correction", "authorized_goal"}
)
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
TERMINAL = frozenset(
    {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled}
)


class SupervisorError(RuntimeError):
    pass


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
        if any("out_of_scope" in item.reason for item in assessments):
            return (
                None,
                "OUT_OF_SCOPE",
                "candidate repository/path scope is unauthorized",
            )
        if any("capability" in item.reason for item in assessments):
            return (
                None,
                "CAPABILITY_MISSING",
                "a bounded candidate has a durable capability gap",
            )
        if any("provider" in item.reason for item in assessments):
            return None, "WAITING_PROVIDER", "provider-dependent work is waiting"
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
):
    """Select and execute bounded existing work; every effect remains below Safe Planner."""
    bounds = bounds or SupervisorBounds()
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
    binding_map = {binding.task_id: binding for binding in bindings}
    last_candidates = ()
    deferred: dict[uuid.UUID, str] = {}
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
        assessments = discover_candidates(
            db, scope=scope, bindings=bindings, bounds=bounds
        )
        assessments = tuple(
            replace(item, actionable=False, reason=deferred[item.task_id])
            if item.task_id in deferred
            else item
            for item in assessments
        )
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
            or not set(context.allowed_paths).issubset(set(scope.allowed_paths))
        ):
            raise SupervisorError(
                "execution context escapes authorized owner/repository scope"
            )
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
        if binding.candidate is not None:
            planning = plan_founder_request(
                db,
                request=request,
                operator_context=context,
                candidate=binding.candidate,
            )
        else:
            planning = await plan_with_provider(
                db,
                request=request,
                operator_context=context,
                adapter=binding.provider_adapter,
            )
        if planning.classification != "ACCEPTED" or planning.plan is None:
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
                },
            )
            if planning.classification == "WAITING_PROVIDER" and binding.independent:
                deferred[task.id] = "provider planning is unavailable"
                continue
            if planning.classification == "CAPABILITY_MISSING" and binding.independent:
                deferred[task.id] = "capability missing in provider plan"
                continue
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
        driver = run_driver(db, context=context, plan=planning.plan)
        if driver.classification != "COMPLETE":
            if driver.classification == "VERIFICATION_REQUIRED":
                task.blocker_reason = (
                    "verification failed or remains incomplete; bounded repair required"
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
                    if driver.classification == "VERIFICATION_REQUIRED"
                    else None,
                },
            )
            if driver.classification == "CAPABILITY_MISSING" and binding.independent:
                deferred[task.id] = (
                    f"capability missing: {driver.detail.get('capability', 'unknown')}"
                )
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
                if driver.classification == "VERIFICATION_REQUIRED"
                else None,
            )
        selected.append(task.id)
        completed_jobs += 1
        jobs_this_run += 1
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
