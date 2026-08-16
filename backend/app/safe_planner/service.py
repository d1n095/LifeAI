"""Fail-closed founder-intent planning for the bounded Development Work Driver."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from sqlalchemy import select

from app.development_driver.service import DevelopmentPlan, DriverStep, validate_plan
from app.development_operator import service as operator
from app.intelligence_governance import record_evidence
from app.mainai_execution.approval import require_task_approval
from app.mainai_execution.checkpoint import latest_checkpoint_for_step, record_checkpoint
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.mainai_job import MainAIJob
from app.models.work_intelligence import WorkStrategyExecution


class SafePlannerError(RuntimeError):
    pass


class CandidateValidationError(SafePlannerError):
    pass


AUTHORIZED_KINDS = frozenset(
    {"founder_requirement", "founder_decision", "founder_correction", "authorized_goal"}
)
NON_AUTHORITATIVE_KINDS = frozenset(
    {"founder_preference", "idea", "suggestion", "hypothesis", "ai_interpretation", "unknown"}
)
FORBIDDEN_WORDS = re.compile(
    r"\b(shell|terminal|merge|deploy|force[- ]?push|production|prod(?:uction)? database)\b",
    re.IGNORECASE,
)
UNBOUNDED_WORDS = re.compile(
    r"\b(fix everything|whatever commands|anything needed|keep going forever|no limits)\b",
    re.IGNORECASE,
)
PATH_KEYS = frozenset({"path", "scope", "target"})
MAX_INSTRUCTION_CHARS = 8_000


@dataclass(frozen=True)
class FounderPlanningRequest:
    owner_id: uuid.UUID
    goal_id: uuid.UUID
    task_id: uuid.UUID
    job_id: uuid.UUID
    original_instruction: str
    authority_kind: str
    authority_basis: str = "manual"
    source_ref: str = "mainai_goal.original_instruction"
    requested_outcome: str = ""
    constraints: tuple[str, ...] = ()
    prohibitions: tuple[str, ...] = ()
    superseded: bool = False
    ambiguity_refs: tuple[str, ...] = ()
    contradiction_refs: tuple[str, ...] = ()
    deterministic_resolutions: tuple[str, ...] = ()
    repository_identity: str | None = None
    max_context_members: int = 30
    max_plan_steps: int = 20
    max_alternatives: int = 3
    max_replans: int = 3
    max_provider_attempts: int = 1
    max_elapsed_seconds: int = 60


@dataclass(frozen=True)
class CandidateStep:
    step_id: str
    purpose: str
    expected_result: str
    capability: str
    arguments: dict = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    required_risk: str = operator.READ_ONLY
    expected_evidence: str = "operator_trace"
    verification_required: bool = False
    on_failure: str = "stop"
    independent_of_provider: bool = True


@dataclass(frozen=True)
class PlanAlternative:
    name: str
    benefit: str
    risk: str
    required_capabilities: tuple[str, ...] = ()
    verification_implications: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanCandidate:
    interpretation: str
    requested_outcome: str
    rationale: str
    steps: tuple[CandidateStep, ...]
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    alternatives: tuple[PlanAlternative, ...] = ()
    strategy_refs: tuple[str, ...] = ()
    lesson_refs: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None
    model_version: str | None = None
    provider_role: str | None = None
    predecessor_plan_id: str | None = None
    replan_reason: str | None = None


@dataclass(frozen=True)
class PlanningResult:
    classification: str
    explanation: dict
    plan: DevelopmentPlan | None = None
    checkpoint_id: uuid.UUID | None = None
    context_set_id: uuid.UUID | None = None


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _candidate_hash(candidate: PlanCandidate) -> str:
    return hashlib.sha256(_canonical(asdict(candidate)).encode()).hexdigest()


def _load_scope(db, request: FounderPlanningRequest):
    goal = db.execute(
        select(MainAIGoal).where(
            MainAIGoal.id == request.goal_id,
            MainAIGoal.owner_id == request.owner_id,
        )
    ).scalar_one_or_none()
    task = db.execute(
        select(MainAITask).where(
            MainAITask.id == request.task_id,
            MainAITask.goal_id == request.goal_id,
            MainAITask.owner_id == request.owner_id,
        )
    ).scalar_one_or_none()
    job = db.execute(
        select(MainAIJob).where(
            MainAIJob.id == request.job_id,
            MainAIJob.owner_id == request.owner_id,
        )
    ).scalar_one_or_none()
    if goal is None or task is None or job is None or task.mainai_job_id != job.id:
        raise CandidateValidationError("goal/task/job scope is missing or cross-owner")
    if goal.original_instruction != request.original_instruction:
        raise CandidateValidationError(
            "founder source text differs from canonical MainAI goal instruction"
        )
    return goal, task, job


def _checkpoint(db, request, goal, task, classification, detail):
    return record_checkpoint(
        db,
        task=task,
        goal=goal,
        job_id=request.job_id,
        step="safe_planner",
        data={"classification": classification, "planning": detail},
    )


def assess_authority(db, request: FounderPlanningRequest) -> PlanningResult | None:
    goal, task, _ = _load_scope(db, request)
    if not request.original_instruction.strip() or len(request.original_instruction) > MAX_INSTRUCTION_CHARS:
        raise CandidateValidationError("founder instruction is empty or exceeds planning bounds")
    unresolved_ambiguity = sorted(set(request.ambiguity_refs) - set(request.deterministic_resolutions))
    unresolved_conflicts = sorted(set(request.contradiction_refs) - set(request.deterministic_resolutions))
    if request.superseded:
        classification = "NEEDS_AUTHORIZATION"
        reason = "the referenced founder instruction is superseded"
    elif request.authority_kind in NON_AUTHORITATIVE_KINDS:
        classification = "NEEDS_AUTHORIZATION"
        reason = f"{request.authority_kind} is not execution authority"
    elif request.authority_kind not in AUTHORIZED_KINDS:
        classification = "NEEDS_AUTHORIZATION"
        reason = "unknown authority kind fails closed"
    elif unresolved_conflicts:
        classification = "CONTRADICTION_UNRESOLVED"
        reason = "current authority references conflict"
    elif unresolved_ambiguity:
        classification = "NEEDS_CLARIFICATION"
        reason = "materially different interpretations remain"
    elif UNBOUNDED_WORDS.search(request.original_instruction):
        classification = "NEEDS_CLARIFICATION"
        reason = "instruction has no deterministic bounded scope"
    else:
        return None
    detail = {
        "reason": reason,
        "authority_kind": request.authority_kind,
        "source_ref": operator.redact(request.source_ref),
        "ambiguities": unresolved_ambiguity,
        "contradictions": unresolved_conflicts,
        "question": "Which bounded outcome and repository scope should Life use?",
        "source_sha256": hashlib.sha256(request.original_instruction.encode()).hexdigest(),
    }
    cp = _checkpoint(db, request, goal, task, classification, detail)
    return PlanningResult(classification, detail, checkpoint_id=cp.id)


def assemble_planning_context(db, request: FounderPlanningRequest):
    from app.development_driver.service import assemble_context

    if not 1 <= request.max_context_members <= 100:
        raise CandidateValidationError("context-member bound is invalid")
    context_set, members = assemble_context(
        db,
        owner_id=request.owner_id,
        task_id=request.task_id,
        max_depth=3,
        max_members=request.max_context_members,
    )
    references = [
        {
            "object_type": member.object_type,
            "object_ref": member.object_ref,
            "basis": member.relevance_basis,
            "reason": member.inclusion_reason,
            "path": member.activation_path,
        }
        for member in members
    ]
    return context_set, references


def parse_candidate(payload: dict) -> PlanCandidate:
    if not isinstance(payload, dict):
        raise CandidateValidationError("plan candidate must be an object")
    required = {"interpretation", "requested_outcome", "rationale", "steps"}
    if not required.issubset(payload):
        raise CandidateValidationError("plan candidate lacks required fields")
    allowed_top = required | {
        "assumptions", "unknowns", "facts", "exclusions", "alternatives",
        "strategy_refs", "lesson_refs", "provider", "model", "model_version",
        "provider_role", "predecessor_plan_id", "replan_reason",
    }
    if set(payload) - allowed_top:
        raise CandidateValidationError("plan candidate contains unknown fields")
    if not isinstance(payload["steps"], list):
        raise CandidateValidationError("plan candidate steps must be a list")
    steps = []
    for index, raw in enumerate(payload["steps"]):
        if not isinstance(raw, dict):
            raise CandidateValidationError(f"step {index} must be an object")
        allowed = {
            "step_id", "purpose", "expected_result", "capability", "arguments",
            "depends_on", "required_risk", "expected_evidence",
            "verification_required", "on_failure", "independent_of_provider",
        }
        if set(raw) - allowed:
            raise CandidateValidationError(f"step {index} contains unknown fields")
        try:
            steps.append(
                CandidateStep(
                    step_id=str(raw["step_id"]),
                    purpose=str(raw["purpose"]),
                    expected_result=str(raw["expected_result"]),
                    capability=str(raw["capability"]),
                    arguments=dict(raw.get("arguments", {})),
                    depends_on=tuple(raw.get("depends_on", ())),
                    required_risk=str(raw.get("required_risk", operator.READ_ONLY)),
                    expected_evidence=str(raw.get("expected_evidence", "operator_trace")),
                    verification_required=bool(raw.get("verification_required", False)),
                    on_failure=str(raw.get("on_failure", "stop")),
                    independent_of_provider=bool(raw.get("independent_of_provider", True)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CandidateValidationError(f"malformed step {index}") from exc
    alternatives = tuple(
        PlanAlternative(
            name=str(item["name"]),
            benefit=str(item["benefit"]),
            risk=str(item["risk"]),
            required_capabilities=tuple(item.get("required_capabilities", ())),
            verification_implications=tuple(item.get("verification_implications", ())),
        )
        for item in payload.get("alternatives", ())
    )
    return PlanCandidate(
        interpretation=str(payload["interpretation"]),
        requested_outcome=str(payload["requested_outcome"]),
        rationale=str(payload["rationale"]),
        steps=tuple(steps),
        assumptions=tuple(payload.get("assumptions", ())),
        unknowns=tuple(payload.get("unknowns", ())),
        facts=tuple(payload.get("facts", ())),
        exclusions=tuple(payload.get("exclusions", ())),
        alternatives=alternatives,
        strategy_refs=tuple(payload.get("strategy_refs", ())),
        lesson_refs=tuple(payload.get("lesson_refs", ())),
        provider=payload.get("provider"),
        model=payload.get("model"),
        model_version=payload.get("model_version"),
        provider_role=payload.get("provider_role"),
        predecessor_plan_id=payload.get("predecessor_plan_id"),
        replan_reason=payload.get("replan_reason"),
    )


def _ordered_steps(candidate: PlanCandidate) -> list[CandidateStep]:
    by_id = {step.step_id: step for step in candidate.steps}
    if len(by_id) != len(candidate.steps) or any(not key.strip() for key in by_id):
        raise CandidateValidationError("step identities must be non-empty and unique")
    for step in candidate.steps:
        if any(dep not in by_id for dep in step.depends_on):
            raise CandidateValidationError("step dependency references an unknown step")
    result: list[CandidateStep] = []
    done: set[str] = set()
    while len(result) < len(candidate.steps):
        ready = [
            step for step in candidate.steps
            if step.step_id not in done and set(step.depends_on).issubset(done)
        ]
        if not ready:
            raise CandidateValidationError("plan dependency graph contains a cycle")
        for step in ready:
            result.append(step)
            done.add(step.step_id)
    return result


def _validate_path_value(context: operator.OperatorContext, value: str):
    operator._path(context, value)


def _validate_step(context: operator.OperatorContext, step: CandidateStep):
    from app.development_driver.service import DRIVER_DIRECTIVES

    if step.capability in DRIVER_DIRECTIVES:
        if step.capability != "verification_evaluate":
            raise CandidateValidationError(
                "provider/review directives require an explicit planning wait, not execution"
            )
        if step.arguments:
            raise CandidateValidationError("verification directive accepts no arguments")
        return
    if step.capability in {
        "shell", "raw_shell", "merge", "deploy", "production_mutation", "force_push"
    }:
        raise CandidateValidationError(f"forbidden capability: {step.capability}")
    if step.capability not in operator.DEVELOPMENT_CAPABILITIES:
        raise operator.OperatorCapabilityMissing(
            step.capability,
            why_needed=step.purpose,
            unrelated_work_can_continue=step.independent_of_provider,
        )
    if step.capability in {"push_branch"}:
        raise CandidateValidationError("remote-write planning is outside this foundation")
    if any(key in step.arguments for key in {"command", "shell", "argv", "executable"}):
        raise CandidateValidationError("raw command-shaped plan arguments are forbidden")
    if step.on_failure not in {"stop", "retry"}:
        raise CandidateValidationError("unknown failure behavior")
    actual_risk = operator.DEVELOPMENT_CAPABILITIES[step.capability]
    risk_order = {
        operator.READ_ONLY: 0,
        operator.LOCAL_WRITE: 1,
        operator.LOCAL_EXECUTION: 2,
        operator.REMOTE_WRITE: 3,
    }
    if step.required_risk not in risk_order or risk_order[actual_risk] > risk_order[step.required_risk]:
        raise CandidateValidationError("candidate understates capability risk")
    for key, value in step.arguments.items():
        if key in PATH_KEYS and isinstance(value, str):
            target = value.split("::", 1)[0] if key == "target" else value
            _validate_path_value(context, target)
        if key == "paths":
            if not isinstance(value, list):
                raise CandidateValidationError("paths must be a list")
            for path in value:
                _validate_path_value(context, path)
    redacted = operator._redact_value(step.arguments)
    if redacted != step.arguments:
        raise CandidateValidationError("secret-like material is forbidden in plan arguments")


def validate_candidate(
    db,
    *,
    request: FounderPlanningRequest,
    candidate: PlanCandidate,
    operator_context: operator.OperatorContext,
) -> PlanningResult:
    started = time.monotonic()
    goal, task, _ = _load_scope(db, request)
    authority_result = assess_authority(db, request)
    if authority_result:
        return authority_result
    if operator_context.owner_id != request.owner_id or operator_context.task_id != request.task_id or operator_context.job_id != request.job_id:
        raise CandidateValidationError("operator context differs from planning scope")
    if request.repository_identity and request.repository_identity != str(operator_context.repository_root.resolve()):
        raise CandidateValidationError("repository identity differs from authorized workspace")
    if not 1 <= len(candidate.steps) <= min(request.max_plan_steps, 50):
        raise CandidateValidationError("plan exceeds bounded step count")
    if len(candidate.alternatives) > request.max_alternatives:
        raise CandidateValidationError("plan exceeds bounded alternative count")
    if bool(candidate.predecessor_plan_id) != bool(candidate.replan_reason):
        raise CandidateValidationError("plan revision requires predecessor and reason together")
    if FORBIDDEN_WORDS.search(candidate.rationale) or FORBIDDEN_WORDS.search(candidate.interpretation):
        raise CandidateValidationError("candidate interpretation contains forbidden action")
    ordered = _ordered_steps(candidate)
    for step in ordered:
        _validate_step(operator_context, step)
    if time.monotonic() - started > request.max_elapsed_seconds:
        raise CandidateValidationError("planning elapsed-time bound exceeded")
    # Safe Planner is reached only from the autonomous chain (development_supervisor), never
    # from a human-reviewed goal dispatched through the ordinary executor -- fail closed the
    # same way development_driver.validate_plan() does rather than relying on this check
    # happening to also run there later in the call chain (founder P1 review finding: an
    # autonomous goal must never silently inherit the human-reviewed "standard_repo_work"
    # AUTO default).
    if goal.approval_policy != "autonomous_development_work":
        raise CandidateValidationError(
            "autonomous safe planner requires goal.approval_policy == "
            "'autonomous_development_work' -- refusing to plan local writes/commits under a "
            f"policy ('{goal.approval_policy}') not scoped for unattended execution"
        )
    require_task_approval(db, task=task, goal_approval_policy=goal.approval_policy)
    candidate_hash = _candidate_hash(candidate)
    plan = DevelopmentPlan(
        plan_id=f"safe-{candidate_hash[:24]}",
        steps=tuple(
            DriverStep(
                capability=step.capability,
                purpose=step.purpose,
                expected_result=step.expected_result,
                arguments=step.arguments,
                required_risk=step.required_risk,
                expected_evidence=step.expected_evidence,
                verification_required=step.verification_required,
                on_failure=step.on_failure,
                independent_of_provider=step.independent_of_provider,
            )
            for step in ordered
        ),
        strategy_execution_id=operator_context.strategy_execution_id,
    )
    validate_plan(db, operator_context, plan)
    context_set, context_refs = assemble_planning_context(db, request)
    prior = latest_checkpoint_for_step(
        db, task_id=task.id, job_id=request.job_id, step="safe_planner"
    )
    prior_data = prior.executor_state.get("planning", {}) if prior else {}
    same_source = prior_data.get("source_sha256") == hashlib.sha256(
        request.original_instruction.encode()
    ).hexdigest()
    if same_source and prior_data.get("candidate_hash") not in {None, candidate_hash}:
        is_revision = (
            candidate.predecessor_plan_id == prior_data.get("plan_id")
            and bool(candidate.replan_reason)
        )
        if not is_revision:
            raise CandidateValidationError(
                "equivalent planning identity conflicts with prior candidate"
            )
    detail = {
        "source_ref": operator.redact(request.source_ref),
        "source_sha256": hashlib.sha256(request.original_instruction.encode()).hexdigest(),
        "authority_kind": request.authority_kind,
        "authority_basis": request.authority_basis,
        "interpretation": candidate.interpretation,
        "requested_outcome": candidate.requested_outcome,
        "problem_framing": {
            "facts": list(candidate.facts),
            "assumptions": list(candidate.assumptions),
            "unknowns": list(candidate.unknowns),
            "constraints": list(request.constraints),
            "prohibitions": list(request.prohibitions),
        },
        "candidate_hash": candidate_hash,
        "plan_id": plan.plan_id,
        "ordered_step_ids": [step.step_id for step in ordered],
        "capabilities": [step.capability for step in ordered],
        "verification_required": [step.step_id for step in ordered if step.verification_required],
        "alternatives": [asdict(item) for item in candidate.alternatives],
        "strategy_refs": list(candidate.strategy_refs),
        "lesson_refs": list(candidate.lesson_refs),
        "provider_attribution": {
            "provider": candidate.provider,
            "model": candidate.model,
            "model_version": candidate.model_version,
            "role": candidate.provider_role,
            "disposition": "accepted_with_deterministic_validation" if candidate.provider else "not_used",
        },
        "predecessor_plan_id": candidate.predecessor_plan_id,
        "replan_reason": candidate.replan_reason,
        "context_set_id": str(context_set.id),
        "context_refs": context_refs,
        "stop_conditions": [
            "capability_missing", "approval_required", "verification_failed",
            "lease_lost", "workspace_changed", "action_bound", "ambiguity",
        ],
    }
    binding = db.execute(
        select(WorkStrategyExecution).where(
            WorkStrategyExecution.id == operator_context.strategy_execution_id,
            WorkStrategyExecution.owner_id == request.owner_id,
        )
    ).scalar_one()
    evidence = record_evidence(
        db,
        owner_id=request.owner_id,
        execution_id=binding.execution_id,
        evidence_kind="safe_plan_accepted",
        payload={
            "candidate_hash": candidate_hash,
            "plan_id": plan.plan_id,
            "capabilities": detail["capabilities"],
            "required_verification_steps": detail["verification_required"],
            "provider_used": bool(candidate.provider),
        },
        source_type="mainai_goal",
        source_ref=str(request.goal_id),
        idempotency_key=f"safe-planner:{candidate_hash}",
        deterministic=True,
        review_kind="deterministic_tool",
    )
    detail["intelligence_evidence_id"] = str(evidence.id)
    if same_source and prior_data.get("candidate_hash") == candidate_hash:
        return PlanningResult(
            "ACCEPTED", prior_data, plan, prior.id, context_set.id
        )
    cp = _checkpoint(db, request, goal, task, "ACCEPTED", detail)
    return PlanningResult("ACCEPTED", detail, plan, cp.id, context_set.id)


def record_capability_gap(db, *, request, operator_context, capability, why_needed, unrelated_work_can_continue=True):
    goal, task, _ = _load_scope(db, request)
    detail = {
        "requested_capability": capability,
        "why_needed": why_needed,
        "unrelated_work_can_continue": unrelated_work_can_continue,
        "task_id": str(request.task_id),
        "job_id": str(request.job_id),
        "repository": str(operator_context.repository_root),
    }
    cp = _checkpoint(db, request, goal, task, "CAPABILITY_MISSING", detail)
    return PlanningResult("CAPABILITY_MISSING", detail, checkpoint_id=cp.id)


def safe_provider_prompt(request: FounderPlanningRequest, context_refs: list[dict]) -> dict:
    """Return bounded, redacted provider input; source remains canonical in MainAIGoal."""
    instruction = operator.redact(request.original_instruction)
    if instruction != request.original_instruction:
        instruction = "[FOUNDER INSTRUCTION CONTAINS REDACTED SENSITIVE MATERIAL]"
    return {
        "instruction": instruction,
        "source_ref": operator.redact(request.source_ref),
        "context_references": operator._redact_value(
            context_refs[: request.max_context_members]
        ),
        "capabilities": sorted(operator.DEVELOPMENT_CAPABILITIES),
        "forbidden": ["raw_shell", "merge", "deploy", "force_push", "production_mutation"],
        "required_output": "strict PlanCandidate JSON",
        "limits": {"max_steps": request.max_plan_steps, "max_alternatives": request.max_alternatives},
    }


def deterministic_candidate(instruction: str, operator_context=None) -> PlanCandidate | None:
    """A tiny explicit recipe registry, not semantic inference or a generic prose executor.

    Only the historical subtract recipe is matched here so `plan_with_provider()` planning_mode
    continues to require provider assistance for ordinary multiplication goals. Gap-child
    handoff uses `build_multiplication_repair_candidate()` directly."""
    del operator_context  # reserved for callers that already selected a concrete recipe
    normalized = " ".join(instruction.lower().split())
    if "calculator" not in normalized or "subtract" not in normalized or "focused test" not in normalized:
        return None
    source = "def subtract(left, right):\n    return left - right\n"
    test = "from calculator import subtract\n\ndef test_subtract():\n    assert subtract(7, 2) == 5\n"
    return PlanCandidate(
        interpretation="Add one deterministic integer subtraction helper and its focused test.",
        requested_outcome="A verified subtract helper in the authorized disposable repository.",
        rationale="Use the registered bounded calculator recipe; change only source and focused test.",
        facts=("The requested behavior is deterministic integer subtraction.",),
        assumptions=("calculator.py and test_calculator.py are authorized new paths.",),
        exclusions=("No merge, deploy, push, dependency installation, or unrelated edits.",),
        steps=(
            CandidateStep("source", "create subtraction helper", "source hash", "create_file", {"path": "calculator.py", "content": source, "expected_sha256": None}, required_risk=operator.LOCAL_WRITE),
            CandidateStep("test", "create focused subtraction test", "test hash", "create_file", {"path": "test_calculator.py", "content": test, "expected_sha256": None}, depends_on=("source",), required_risk=operator.LOCAL_WRITE),
            CandidateStep("verify", "run focused subtraction test", "pytest exit zero", "run_focused_test", {"profile_name": "focused_pytest", "arguments": ["test_calculator.py"]}, depends_on=("test",), required_risk=operator.LOCAL_EXECUTION, verification_required=True),
            CandidateStep("verification-gate", "evaluate required deterministic evidence", "verification checkpoint", "verification_evaluate", depends_on=("verify",)),
        ),
    )


def build_multiplication_repair_candidate(operator_context=None) -> PlanCandidate:
    """Structured multiply repair/implement recipe for the autonomous-gap live handoff."""
    test = (
        "from calculator import multiply\n\n"
        "def test_multiply():\n"
        "    assert multiply(3, 4) == 12\n"
    )
    original = "def add(left, right):\n    return left + right\n"
    test_exists = False
    test_before = None
    if operator_context is not None:
        calc = operator_context.repository_root / "calculator.py"
        if calc.is_file():
            original = calc.read_text(encoding="utf-8")
        test_path = operator_context.repository_root / "test_calculator.py"
        if test_path.is_file():
            test_exists = True
            test_before = hashlib.sha256(test_path.read_bytes()).hexdigest()
    if "def multiply" in original and "return left * right" in original:
        updated = original
    elif "def multiply" in original:
        updated = original.split("def multiply", 1)[0].rstrip() + (
            "\n\ndef multiply(left, right):\n    return left * right\n"
        )
    else:
        updated = original.rstrip() + "\n\ndef multiply(left, right):\n    return left * right\n"
    expected = hashlib.sha256(original.encode()).hexdigest()
    test_step = CandidateStep(
        "test",
        "create or repair focused multiplication test",
        "test hash",
        "patch_file" if test_exists else "create_file",
        {
            "path": "test_calculator.py",
            "content": test,
            "expected_sha256": test_before,
        },
        depends_on=("patch",),
        required_risk=operator.LOCAL_WRITE,
    )
    return PlanCandidate(
        interpretation="Add one deterministic integer multiplication helper and its focused test.",
        requested_outcome="A verified multiply helper in the authorized disposable repository.",
        rationale="Use the registered bounded calculator multiplication recipe.",
        facts=("The requested behavior is deterministic integer multiplication.",),
        assumptions=("calculator.py already exists; test_calculator.py is an authorized path.",),
        exclusions=("No merge, deploy, push, dependency installation, or unrelated edits.",),
        steps=(
            CandidateStep(
                "patch",
                "append or repair multiplication helper",
                "source hash",
                "patch_file",
                {
                    "path": "calculator.py",
                    "content": updated,
                    "expected_sha256": expected,
                },
                required_risk=operator.LOCAL_WRITE,
            ),
            test_step,
            CandidateStep(
                "verify",
                "run focused multiplication test",
                "pytest exit zero",
                "run_focused_test",
                {"profile_name": "focused_pytest", "arguments": ["test_calculator.py"]},
                depends_on=("test",),
                required_risk=operator.LOCAL_EXECUTION,
                verification_required=True,
            ),
            CandidateStep(
                "gate",
                "evaluate required deterministic evidence",
                "verification checkpoint",
                "verification_evaluate",
                depends_on=("verify",),
            ),
            CandidateStep(
                "stage",
                "stage scoped changes",
                "staged diff",
                "stage_scoped_changes",
                {"paths": ["calculator.py", "test_calculator.py"]},
                depends_on=("gate",),
                required_risk=operator.LOCAL_WRITE,
            ),
            CandidateStep(
                "commit",
                "commit scoped changes",
                "commit sha",
                "commit_scoped_changes",
                {"message": "Add multiplication helper"},
                depends_on=("stage",),
                required_risk=operator.LOCAL_WRITE,
            ),
        ),
    )


def plan_founder_request(db, *, request, operator_context, candidate=None):
    authority = assess_authority(db, request)
    if authority:
        return authority
    if FORBIDDEN_WORDS.search(request.original_instruction):
        goal, task, _ = _load_scope(db, request)
        detail = {"reason": "founder text requests forbidden operational effects", "forbidden": FORBIDDEN_WORDS.findall(request.original_instruction), "safe_subset": None}
        cp = _checkpoint(db, request, goal, task, "UNSAFE_REQUEST", detail)
        return PlanningResult("UNSAFE_REQUEST", detail, checkpoint_id=cp.id)
    selected = candidate or deterministic_candidate(
        request.original_instruction, operator_context
    )
    if selected is None:
        goal, task, _ = _load_scope(db, request)
        detail = {"reason": "deterministic planning recipe is unavailable", "unresolved": request.requested_outcome or request.original_instruction[:500]}
        cp = _checkpoint(db, request, goal, task, "WAITING_PROVIDER", detail)
        return PlanningResult("WAITING_PROVIDER", detail, checkpoint_id=cp.id)
    try:
        return validate_candidate(db, request=request, candidate=selected, operator_context=operator_context)
    except operator.OperatorCapabilityMissing as exc:
        return record_capability_gap(db, request=request, operator_context=operator_context, capability=exc.capability, why_needed=exc.why_needed, unrelated_work_can_continue=exc.unrelated_work_can_continue)
