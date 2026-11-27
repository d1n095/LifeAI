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
    try:
        operator._path(context, value)
    except operator.OperatorPathError as exc:
        # Planning must fail closed as CandidateValidationError, not abort the Supervisor tick.
        raise CandidateValidationError(str(exc)) from exc


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


_CALCULATOR_SOURCE_PATH = "calculator.py"
_CALCULATOR_TEST_PATH = "test_calculator.py"

# F2: the bounded operations this demo recipe registry can produce. Only these exact names
# ever become code -- nothing is parsed out of prose into an arbitrary symbol, and every path
# still has to be inside the OperatorContext envelope (validate_candidate() re-checks it).
_CALCULATOR_OPERATIONS = {
    "subtract": ("-", "7, 2", "5"),
    "multiply": ("*", "3, 4", "12"),
}


def _recipe_scope(requested_outcome: str | None) -> str:
    """Which half of a two-part change THIS task owns: "source", "test" or "both".

    F2 (measured 2026-11-27): the deterministic recipe was GOAL-scoped -- every `repo_edit`
    under a goal rewrote the same two files, so a plan whose first task implements the function
    and whose second task adds its focused test had the second one repeat the first one's work
    (and, between tasks, the worktree reset discarded nothing only because the first task had
    already committed both files).

    The ONLY input here is the task's own structured description (`MainAITask.description`,
    passed as `FounderPlanningRequest.requested_outcome`) -- never the goal's prose, and never
    as authority: it selects WHICH of the recipe's already-bounded steps to run, and every
    selected path is still validated against the envelope. A description that names both
    halves, or neither, keeps the previous full-change behaviour.
    """
    text = " ".join((requested_outcome or "").lower().split())
    if not text:
        return "both"
    mentions_test = "test" in text or "pytest" in text
    mentions_source = any(
        token in text
        for token in (
            "calculator.py",
            "funktion",
            "function",
            "helper",
            "implementera",
            "implementation",
        )
    )
    if mentions_test and not mentions_source:
        return "test"
    if mentions_source and not mentions_test:
        return "source"
    return "both"


def _read_worktree_text(operator_context, path: str) -> str | None:
    if operator_context is None:
        return None
    target = operator_context.repository_root / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def _ensure_line(text: str, line: str) -> str:
    if line in text:
        return text
    return text.rstrip() + "\n\n" + line + "\n"


def _upsert_function(text: str | None, name: str, block: str) -> str:
    """Append the function, or REPLACE an existing definition of the same name in place.

    Cumulative and repairing at the same time: an earlier task's other helpers stay exactly
    where they are (never rewritten, never dropped), while a definition this task owns is
    corrected if it is already present -- which is what a repair child needs (the bounded
    multiply recipe has always repaired a wrong `multiply` body, and losing that would turn a
    repairable failure into a permanent one).

    Bounded by construction: only the `def <name>(` line and the indented/blank lines that
    follow it are replaced; everything else in the file is untouched.
    """
    if text is None or not text.strip():
        return block
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith(f"def {name}(")), None)
    if start is None:
        return text.rstrip() + "\n\n" + block
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "\t"))):
        end += 1
    updated = "\n".join(lines[:start] + block.rstrip("\n").splitlines() + lines[end:])
    return updated + ("\n" if text.endswith("\n") else "")


def _calculator_recipe_candidate(
    *,
    operation: str,
    operator_context,
    requested_outcome: str | None,
    interpretation: str,
    requested_result: str,
    rationale: str,
) -> PlanCandidate:
    """One bounded calculator change, scoped to the task that asked for it.

    Task-scoped, not goal-scoped (F2): a task that names only the implementation writes only
    `calculator.py` and verifies it statically; a task that names only the test appends only
    its test to `test_calculator.py` and runs it. Both are cumulative -- the earlier task's
    committed work is never rewritten -- and neither invents a test file for a source-only
    task, so the plan's own ordering (implement, then test) is preserved rather than
    duplicated.
    """
    symbol, sample, expected = _CALCULATOR_OPERATIONS[operation]
    scope = _recipe_scope(requested_outcome)
    source_before = _read_worktree_text(operator_context, _CALCULATOR_SOURCE_PATH)
    test_before = _read_worktree_text(operator_context, _CALCULATOR_TEST_PATH)

    function_block = f"def {operation}(left, right):\n    return left {symbol} right\n"
    test_definition = (
        f"def test_{operation}():\n"
        f"    assert {operation}({sample}) == {expected}\n"
    )

    steps: list[CandidateStep] = []
    staged: list[str] = []
    if scope in {"source", "both"}:
        content = _upsert_function(source_before, operation, function_block)
        if content != source_before:
            steps.append(
                CandidateStep(
                    "source",
                    f"add the {operation} helper",
                    "source hash",
                    "create_file" if source_before is None else "patch_file",
                    {
                        "path": _CALCULATOR_SOURCE_PATH,
                        "content": content,
                        "expected_sha256": (
                            hashlib.sha256(source_before.encode()).hexdigest()
                            if source_before is not None
                            else None
                        ),
                    },
                    required_risk=operator.LOCAL_WRITE,
                )
            )
            staged.append(_CALCULATOR_SOURCE_PATH)
    if scope in {"test", "both"}:
        content = _upsert_function(
            _ensure_line(test_before or "", f"from calculator import {operation}"),
            f"test_{operation}",
            test_definition,
        )
        if content != test_before:
            steps.append(
                CandidateStep(
                    "test",
                    f"add the focused {operation} test",
                    "test hash",
                    "create_file" if test_before is None else "patch_file",
                    {
                        "path": _CALCULATOR_TEST_PATH,
                        "content": content,
                        "expected_sha256": (
                            hashlib.sha256(test_before.encode()).hexdigest()
                            if test_before is not None
                            else None
                        ),
                    },
                    depends_on=("source",) if "source" in {step.step_id for step in steps} else (),
                    required_risk=operator.LOCAL_WRITE,
                )
            )
            staged.append(_CALCULATOR_TEST_PATH)

    if not steps:
        # Everything this task asks for is already present (an earlier task delivered it, or
        # this exact task is being retried after a crash). Verify the existing work instead of
        # inventing an empty commit -- `commit_scoped_changes` correctly refuses one.
        return PlanCandidate(
            interpretation=interpretation,
            requested_outcome=requested_result,
            rationale=(
                rationale
                + " The requested change is already present in this worktree, so this attempt "
                "re-verifies it instead of duplicating it."
            ),
            facts=(f"The requested {operation} change is already present.",),
            assumptions=(f"{_CALCULATOR_TEST_PATH} is an authorized path.",),
            exclusions=("No merge, deploy, push, dependency installation, or unrelated edits.",),
            steps=(
                CandidateStep(
                    "verify",
                    "re-run the existing focused test",
                    "pytest exit zero",
                    "run_focused_test",
                    {"profile_name": "focused_pytest", "arguments": [_CALCULATOR_TEST_PATH]},
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
            ),
        )

    allowed_capabilities = set(operator_context.allowed_capabilities or ()) if operator_context is not None else set()
    # Committing is what makes this task's verified work survive the NEXT task's fresh-claim
    # worktree reset -- but it is a real capability the founder must have authorized. An
    # envelope that does not include it gets the previous, uncommitted shape rather than an
    # OUT_OF_SCOPE refusal: the recipe never assumes authority it was not given.
    can_commit = not allowed_capabilities or {
        "stage_scoped_changes",
        "commit_scoped_changes",
    }.issubset(allowed_capabilities)

    if _CALCULATOR_TEST_PATH in staged:
        verify_capability = "run_focused_test"
        verify_result = "pytest exit zero"
        verify_arguments = {"profile_name": "focused_pytest", "arguments": [_CALCULATOR_TEST_PATH]}
    else:
        # Source-only task: the focused test is the plan's NEXT task, so this one verifies the
        # file it actually changed instead of skipping verification or inventing a test.
        verify_capability = "run_static_check"
        verify_result = "static check exit zero"
        verify_arguments = {"profile_name": "ruff_check", "arguments": [_CALCULATOR_SOURCE_PATH]}
    steps.append(
        CandidateStep(
            "verify",
            f"verify the changed {operation} work",
            verify_result,
            verify_capability,
            verify_arguments,
            depends_on=(steps[-1].step_id,),
            required_risk=operator.LOCAL_EXECUTION,
            verification_required=True,
        )
    )
    steps.append(
        CandidateStep(
            "gate",
            "evaluate required deterministic evidence",
            "verification checkpoint",
            "verification_evaluate",
            depends_on=("verify",),
        )
    )
    if can_commit:
        steps.append(
            CandidateStep(
                "stage",
                "stage scoped changes",
                "staged diff",
                "stage_scoped_changes",
                {"paths": staged},
                depends_on=("gate",),
                required_risk=operator.LOCAL_WRITE,
            )
        )
        if scope == "source":
            message = f"Add {operation} helper"
        elif scope == "test":
            message = f"Add focused {operation} test"
        else:
            message = f"Add {operation} helper and its focused test"
        steps.append(
            CandidateStep(
                "commit",
                "commit scoped changes",
                "commit sha",
                "commit_scoped_changes",
                {"message": message},
                depends_on=("stage",),
                required_risk=operator.LOCAL_WRITE,
            )
        )
    return PlanCandidate(
        interpretation=interpretation,
        requested_outcome=requested_result,
        rationale=rationale,
        facts=(f"The requested behavior is deterministic integer {operation}.",),
        assumptions=("calculator.py and test_calculator.py are authorized paths.",),
        exclusions=("No merge, deploy, push, dependency installation, or unrelated edits.",),
        steps=tuple(steps),
    )


def deterministic_candidate(
    instruction: str, operator_context=None, *, requested_outcome: str | None = None
) -> PlanCandidate | None:
    """A tiny explicit recipe registry, not semantic inference or a generic prose executor.

    Only the historical subtract recipe is matched here so `plan_with_provider()` planning_mode
    continues to require provider assistance for ordinary multiplication goals. Gap-child
    handoff uses `build_multiplication_repair_candidate()` directly.

    `requested_outcome` is the TASK's own description (see `_recipe_scope()`); it narrows which
    of the recipe's bounded steps this task performs, never what it is allowed to touch.
    """
    normalized = " ".join(instruction.lower().split())
    if "calculator" not in normalized or "subtract" not in normalized or "focused test" not in normalized:
        return None
    return _calculator_recipe_candidate(
        operation="subtract",
        operator_context=operator_context,
        requested_outcome=requested_outcome,
        interpretation="Add one deterministic integer subtraction helper and its focused test.",
        requested_result="A verified subtract helper in the authorized disposable repository.",
        rationale="Use the registered bounded calculator recipe; change only source and focused test.",
    )


def build_multiplication_repair_candidate(operator_context=None) -> PlanCandidate:
    """Bounded *demo* multiply repair recipe for the autonomous-gap live handoff.

    Proves envelope -> WorkBinding -> registered PlanCandidate wiring. Requires the effective
    OperatorContext path envelope to already authorize both `calculator.py` and
    `test_calculator.py`; never silently broadens authority to write the test file.

    Deliberately NOT task-scoped (F2 scoping applies to `deterministic_candidate()`, the
    ordinary plan path): a repair child is created from a real failure and must produce the
    whole fix, so narrowing it to "only the source" or "only the test" could leave the failure
    in place. The write is still cumulative -- an existing helper/test is preserved, never
    rewritten.
    """
    required_paths = ("calculator.py", "test_calculator.py")
    if operator_context is not None:
        allowed = tuple(operator_context.allowed_paths or ())
        if allowed and not set(required_paths).issubset(set(allowed)):
            raise CandidateValidationError(
                "multiplication_repair requires calculator.py and test_calculator.py in the "
                "effective OperatorContext path envelope; refusing to broaden binding scope"
            )
    return _calculator_recipe_candidate(
        operation="multiply",
        operator_context=operator_context,
        requested_outcome=None,
        interpretation="Add one deterministic integer multiplication helper and its focused test.",
        requested_result="A verified multiply helper in the authorized disposable repository.",
        rationale="Use the registered bounded calculator multiplication recipe.",
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
        request.original_instruction,
        operator_context,
        requested_outcome=request.requested_outcome,
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
