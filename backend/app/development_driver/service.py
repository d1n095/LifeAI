"""Bounded deterministic work driver above MainAI and the Development Operator."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from app.active_context.service import create_context_set, refresh_context
from app.development_operator import service as operator
from app.life_intents.service import evaluate_feasibility
from app.jobs.service import mark_completed
from app.mainai_execution.approval import require_task_approval
from app.mainai_execution.checkpoint import (
    latest_checkpoint_for_step,
    record_checkpoint,
)
from app.mainai_execution.execution_job import _finalize_task_outcome
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.life_intent import LifeIntent


class DriverError(RuntimeError):
    pass


class DriverAmbiguity(DriverError):
    pass


class DriverPlanError(DriverError):
    pass


PHASES = frozenset(
    {
        "ASSESS",
        "PLAN",
        "EXECUTE",
        "VERIFY",
        "REVIEW",
        "LEARN",
        "RE_EVALUATE",
        "WAIT",
        "BLOCKED",
        "COMPLETE",
        "FAILED",
        "CANCELLED",
    }
)

RESULTS = frozenset(
    {
        "SUCCESS",
        "FAILED_RETRYABLE",
        "FAILED_NONRETRYABLE",
        "CAPABILITY_MISSING",
        "WAITING_EXTERNAL",
        "WAITING_PROVIDER",
        "WAITING_APPROVAL",
        "EXTERNAL_REVIEW_REQUIRED",
        "VERIFICATION_REQUIRED",
        "BLOCKED",
        "CANCELLED",
        "ACTION_BOUND_REACHED",
        "COMPLETE",
        "NEEDS_SELECTION",
    }
)

DRIVER_DIRECTIVES = frozenset(
    {"verification_evaluate", "external_review", "provider_step"}
)
FORBIDDEN_CAPABILITIES = frozenset(
    {"shell", "raw_shell", "merge", "deploy", "production_mutation", "force_push"}
)
RISK_ORDER = {
    operator.READ_ONLY: 0,
    operator.LOCAL_WRITE: 1,
    operator.LOCAL_EXECUTION: 2,
    operator.REMOTE_WRITE: 3,
}


@dataclass(frozen=True)
class DriverStep:
    capability: str
    purpose: str
    expected_result: str
    arguments: dict = field(default_factory=dict)
    required_risk: str = operator.READ_ONLY
    expected_evidence: str = "operator_trace"
    verification_required: bool = False
    on_failure: str = "stop"
    independent_of_provider: bool = True


@dataclass(frozen=True)
class DevelopmentPlan:
    plan_id: str
    steps: tuple[DriverStep, ...]
    strategy_execution_id: uuid.UUID
    max_failures: int = 2


@dataclass(frozen=True)
class DriverResult:
    phase: str
    classification: str
    completed_steps: int
    detail: dict
    checkpoint_id: uuid.UUID


def _semantic_plan(plan: DevelopmentPlan) -> dict:
    return {
        "plan_id": plan.plan_id,
        "strategy_execution_id": str(plan.strategy_execution_id),
        "max_failures": plan.max_failures,
        "steps": [asdict(step) for step in plan.steps],
    }


def _plan_hash(plan: DevelopmentPlan) -> str:
    payload = json.dumps(_semantic_plan(plan), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def select_scoped_task(db, *, owner_id, goal_id, life_intent_id=None):
    if life_intent_id is not None:
        linked_intent = db.execute(
            select(LifeIntent).where(
                LifeIntent.id == life_intent_id,
                LifeIntent.owner_id == owner_id,
                LifeIntent.mainai_goal_id == goal_id,
            )
        ).scalar_one_or_none()
        if linked_intent is None:
            return None, {
                "classification": "BLOCKED",
                "reasons": ["intent_missing_cross_owner_or_not_linked_to_goal"],
            }
        feasibility = evaluate_feasibility(
            db, owner_id=owner_id, intent_id=life_intent_id
        )
        if not feasibility.actionable:
            return None, {
                "classification": "BLOCKED",
                "reasons": list(feasibility.reasons),
                "cycles": [list(cycle) for cycle in feasibility.cycles],
            }
    rows = (
        db.execute(
            select(MainAITask)
            .where(
                MainAITask.owner_id == owner_id,
                MainAITask.goal_id == goal_id,
                MainAITask.status.in_(
                    [MainAITaskStatus.ready, MainAITaskStatus.running]
                ),
            )
            .order_by(MainAITask.priority.desc(), MainAITask.created_at.asc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None, {"classification": "BLOCKED", "reasons": ["no_actionable_task"]}
    highest = rows[0].priority
    tied = [row for row in rows if row.priority == highest]
    if len(tied) > 1:
        return None, {
            "classification": "NEEDS_SELECTION",
            "task_ids": [str(row.id) for row in tied],
        }
    return rows[0], {"classification": "SUCCESS"}


def assemble_context(db, *, owner_id, task_id, max_depth=3, max_members=50):
    context = create_context_set(
        db,
        owner_id=owner_id,
        anchor_type="mainai_task",
        anchor_ref=str(task_id),
        idempotency_key=f"development-driver:{task_id}",
        label=None,
        subject_basis="deterministic",
    )
    members = refresh_context(
        db,
        owner_id=owner_id,
        context_set_id=context.id,
        max_depth=max_depth,
        max_members=max_members,
        max_per_type=min(20, max_members),
    )
    return context, members


def validate_plan(db, context: operator.OperatorContext, plan: DevelopmentPlan):
    if not plan.plan_id.strip() or len(plan.plan_id) > 128:
        raise DriverPlanError("plan identity is missing or too long")
    if not 1 <= len(plan.steps) <= 50 or not 0 <= plan.max_failures <= 10:
        raise DriverPlanError("plan/action bounds are invalid")
    if plan.strategy_execution_id != context.strategy_execution_id:
        raise DriverPlanError("plan strategy attribution differs from operator context")
    task, _, binding, _ = operator._require_context(
        db,
        context,
        write=any(step.required_risk != operator.READ_ONLY for step in plan.steps),
    )
    if task.status != MainAITaskStatus.running:
        raise DriverPlanError(
            "development driver requires the current running MainAI task"
        )
    goal = db.execute(
        select(MainAIGoal).where(
            MainAIGoal.id == task.goal_id, MainAIGoal.owner_id == context.owner_id
        )
    ).scalar_one()
    # This driver is reached only from the autonomous chain (development_supervisor /
    # safe_planner) -- a human-reviewed goal dispatched via the ordinary executor never runs
    # through validate_plan(). A goal still carrying the human-reviewed "standard_repo_work"
    # default here would let autonomous writes/commits inherit that policy's AUTO approval
    # silently (founder P1 review finding) -- fail closed instead of trusting the caller set
    # the right policy.
    if goal.approval_policy != "autonomous_development_work":
        raise DriverPlanError(
            "autonomous development driver requires goal.approval_policy == "
            "'autonomous_development_work' -- refusing to run local writes/commits under a "
            f"policy ('{goal.approval_policy}') not scoped for unattended execution"
        )
    require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)
    if binding.id != plan.strategy_execution_id:
        raise DriverPlanError("strategy binding is not canonical")
    for position, step in enumerate(plan.steps):
        if not step.purpose.strip() or not step.expected_result.strip():
            raise DriverPlanError(f"step {position} lacks purpose/expected result")
        if step.capability in FORBIDDEN_CAPABILITIES:
            raise DriverPlanError(f"forbidden capability: {step.capability}")
        if any(key in step.arguments for key in {"command", "shell", "argv"}):
            raise DriverPlanError("raw command-shaped arguments are forbidden")
        if step.capability in DRIVER_DIRECTIVES:
            continue
        actual_risk = operator.DEVELOPMENT_CAPABILITIES.get(step.capability)
        if actual_risk is None:
            continue  # becomes a durable CAPABILITY_MISSING result during execution
        if (
            step.required_risk not in RISK_ORDER
            or RISK_ORDER[actual_risk] > RISK_ORDER[step.required_risk]
        ):
            raise DriverPlanError(
                f"step {position} understates capability risk ({actual_risk})"
            )
    return task, goal


def _checkpoint(db, context, task, goal, *, phase, classification, state):
    if phase not in PHASES or classification not in RESULTS:
        raise DriverError("invalid driver phase/result")
    return record_checkpoint(
        db,
        task=task,
        goal=goal,
        job_id=context.job_id,
        step="development_driver",
        data={
            "phase": phase,
            "classification": classification,
            "driver_state": state,
        },
    )


def _invoke_operator(db, context, step, idempotency_key):
    args = dict(step.arguments)
    capability = step.capability
    if capability.startswith("repo_"):
        return operator.inspect_repository(
            db, context, capability, idempotency_key=idempotency_key, **args
        )
    if capability == "list_files":
        return operator.list_files(db, context, idempotency_key=idempotency_key, **args)
    if capability == "search_text":
        return operator.search_text(
            db, context, idempotency_key=idempotency_key, **args
        )
    if capability == "read_file":
        return operator.read_file(db, context, idempotency_key=idempotency_key, **args)
    if capability in {"create_file", "patch_file", "delete_file"}:
        return operator.write_file(
            db,
            context,
            idempotency_key=idempotency_key,
            delete=capability == "delete_file",
            **args,
        )
    if capability.startswith("run_"):
        return operator.run_profile(
            db, context, idempotency_key=idempotency_key, **args
        )
    if capability == "stage_scoped_changes":
        return operator.stage_scoped_changes(
            db, context, idempotency_key=idempotency_key, **args
        )
    if capability == "commit_scoped_changes":
        return operator.commit_changes(
            db, context, idempotency_key=idempotency_key, **args
        )
    if capability == "push_branch":
        raise DriverPlanError(
            "async remote push is outside this synchronous driver slice"
        )
    return operator.capability_missing(
        db,
        context,
        capability=capability,
        why_needed=step.purpose,
        unrelated_work_can_continue=step.independent_of_provider,
        idempotency_key=idempotency_key,
    )


def run_driver(
    db,
    *,
    context: operator.OperatorContext,
    plan: DevelopmentPlan,
    max_actions: int = 10,
    max_elapsed_seconds: int = 300,
):
    started = time.monotonic()
    task, goal = validate_plan(db, context, plan)
    if max_actions < 1 or max_actions > 50 or max_elapsed_seconds < 1:
        raise DriverPlanError("invocation bounds are invalid")
    job = db.execute(
        select(MainAIJob).where(
            MainAIJob.id == context.job_id, MainAIJob.owner_id == context.owner_id
        )
    ).scalar_one()
    if job.cancel_requested:
        state = {
            "plan_id": plan.plan_id,
            "plan_hash": _plan_hash(plan),
            "strategy_execution_id": str(plan.strategy_execution_id),
            "completed": [],
            "failures": 0,
            "next_step": 0,
            "step_attempts": {},
        }
        cp = _checkpoint(
            db,
            context,
            task,
            goal,
            phase="CANCELLED",
            classification="CANCELLED",
            state=state,
        )
        return DriverResult("CANCELLED", "CANCELLED", 0, {}, cp.id)
    previous = latest_checkpoint_for_step(
        db, task_id=task.id, job_id=job.id, step="development_driver"
    )
    plan_hash = _plan_hash(plan)
    state = {
        "plan_id": plan.plan_id,
        "plan_hash": plan_hash,
        "strategy_execution_id": str(plan.strategy_execution_id),
        "completed": [],
        "failures": 0,
        "next_step": 0,
        "step_attempts": {},
    }
    if previous:
        old = previous.executor_state.get("driver_state", {})
        if old.get("plan_hash") != plan_hash:
            raise DriverPlanError(
                "checkpoint plan identity conflicts with supplied plan"
            )
        state = old
    state.setdefault("step_attempts", {})
    actions = 0
    while state["next_step"] < len(plan.steps):
        if actions >= max_actions or time.monotonic() - started >= max_elapsed_seconds:
            cp = _checkpoint(
                db,
                context,
                task,
                goal,
                phase="EXECUTE",
                classification="ACTION_BOUND_REACHED",
                state=state,
            )
            return DriverResult(
                "EXECUTE", "ACTION_BOUND_REACHED", len(state["completed"]), {}, cp.id
            )
        index = state["next_step"]
        step = plan.steps[index]
        if state.get("pending_provider") and not step.independent_of_provider:
            cp = _checkpoint(
                db,
                context,
                task,
                goal,
                phase="WAIT",
                classification="WAITING_PROVIDER",
                state=state,
            )
            return DriverResult(
                "WAIT",
                "WAITING_PROVIDER",
                len(state["completed"]),
                {"independent_work_remains": False},
                cp.id,
            )
        if step.capability == "provider_step":
            independent_remaining = any(
                candidate.independent_of_provider
                for candidate in plan.steps[index + 1 :]
            )
            state["pending_provider"] = {
                "index": index,
                "unresolved": step.purpose,
            }
            state["next_step"] += 1
            if independent_remaining:
                continue
            cp = _checkpoint(
                db,
                context,
                task,
                goal,
                phase="WAIT",
                classification="WAITING_PROVIDER",
                state={
                    **state,
                    "unresolved": step.purpose,
                    "independent_work_remains": independent_remaining,
                },
            )
            return DriverResult(
                "WAIT",
                "WAITING_PROVIDER",
                len(state["completed"]),
                {"independent_work_remains": independent_remaining},
                cp.id,
            )
        if step.capability == "external_review":
            cp = _checkpoint(
                db,
                context,
                task,
                goal,
                phase="REVIEW",
                classification="EXTERNAL_REVIEW_REQUIRED",
                state={
                    **state,
                    "unresolved": step.purpose,
                    "review_context": step.arguments,
                },
            )
            return DriverResult(
                "REVIEW",
                "EXTERNAL_REVIEW_REQUIRED",
                len(state["completed"]),
                step.arguments,
                cp.id,
            )
        if step.capability == "verification_evaluate":
            required = [
                item for item in state["completed"] if item.get("verification_required")
            ]
            passed = bool(required) and all(
                item["result"] == "succeeded" for item in required
            )
            record_checkpoint(
                db,
                task=task,
                goal=goal,
                job_id=job.id,
                step="verification",
                data={
                    "verification": {"passed": passed},
                    "operator_action_ids": [
                        item["trace_event_id"] for item in required
                    ],
                },
            )
            if not passed:
                cp = _checkpoint(
                    db,
                    context,
                    task,
                    goal,
                    phase="VERIFY",
                    classification="VERIFICATION_REQUIRED",
                    state=state,
                )
                return DriverResult(
                    "VERIFY",
                    "VERIFICATION_REQUIRED",
                    len(state["completed"]),
                    {},
                    cp.id,
                )
            state["completed"].append(
                {"index": index, "capability": step.capability, "result": "succeeded"}
            )
            state["next_step"] += 1
            actions += 1
            continue
        attempt = int(state["step_attempts"].get(str(index), 0))
        idem = (
            f"driver:{task.id}:{plan.plan_id}:{index}:{attempt}:{plan_hash[:16]}"
        )
        try:
            result = _invoke_operator(db, context, step, idem)
        except operator.OperatorCapabilityMissing as exc:
            result = operator.capability_missing(
                db,
                context,
                capability=exc.capability,
                why_needed=exc.why_needed,
                unrelated_work_can_continue=exc.unrelated_work_can_continue,
                idempotency_key=idem,
            )
        completed = {
            "index": index,
            "capability": step.capability,
            "result": result.result,
            "trace_event_id": str(result.trace_event_id),
            "verification_required": step.verification_required,
        }
        state["completed"].append(completed)
        state["step_attempts"][str(index)] = attempt + 1
        state["next_step"] += 1
        actions += 1
        operator.checkpoint_operator_progress(
            db,
            context,
            completed_action_ids=[
                uuid.UUID(item["trace_event_id"])
                for item in state["completed"]
                if item.get("trace_event_id")
            ],
            next_phase="execute",
            unresolved_failures=[],
        )
        if result.result == "capability_missing":
            cp = _checkpoint(
                db,
                context,
                task,
                goal,
                phase="BLOCKED",
                classification="CAPABILITY_MISSING",
                state={**state, "capability_gap": result.detail},
            )
            return DriverResult(
                "BLOCKED",
                "CAPABILITY_MISSING",
                len(state["completed"]),
                result.detail,
                cp.id,
            )
        if result.result not in {"succeeded", "no_result"}:
            state["failures"] += 1
            retryable = (
                state["failures"] <= plan.max_failures and step.on_failure == "retry"
            )
            if retryable:
                state["next_step"] -= 1
            classification = "FAILED_RETRYABLE" if retryable else "FAILED_NONRETRYABLE"
            cp = _checkpoint(
                db,
                context,
                task,
                goal,
                phase="FAILED",
                classification=classification,
                state=state,
            )
            return DriverResult(
                "FAILED", classification, len(state["completed"]), result.detail, cp.id
            )

    if state.get("pending_provider"):
        cp = _checkpoint(
            db,
            context,
            task,
            goal,
            phase="WAIT",
            classification="WAITING_PROVIDER",
            state=state,
        )
        return DriverResult(
            "WAIT",
            "WAITING_PROVIDER",
            len(state["completed"]),
            {"independent_work_remains": False},
            cp.id,
        )
    verification = latest_checkpoint_for_step(
        db, task_id=task.id, job_id=job.id, step="verification"
    )
    passed = bool(
        verification
        and verification.executor_state.get("verification", {}).get("passed")
    )
    if task.verification_plan and not passed:
        cp = _checkpoint(
            db,
            context,
            task,
            goal,
            phase="VERIFY",
            classification="VERIFICATION_REQUIRED",
            state=state,
        )
        return DriverResult(
            "VERIFY", "VERIFICATION_REQUIRED", len(state["completed"]), {}, cp.id
        )
    _finalize_task_outcome(
        db,
        task,
        passed=True,
        evidence={"development_driver_plan": plan.plan_id, "plan_hash": plan_hash},
    )
    cp = _checkpoint(
        db,
        context,
        task,
        goal,
        phase="COMPLETE",
        classification="COMPLETE",
        state=state,
    )
    mark_completed(
        db,
        job,
        worker_id=context.worker_id,
        lease_generation=context.lease_generation,
        public_message="Development Driver completed verified bounded work.",
    )
    return DriverResult("COMPLETE", "COMPLETE", len(state["completed"]), {}, cp.id)
