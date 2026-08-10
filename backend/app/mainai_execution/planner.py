"""Goal -> structured, durable task graph -- the PLANNER step of the MainAI Execution Loop
V0.1 (see app/mainai_execution/__init__.py's module docstring for the full loop).

Two genuinely different steps, deliberately kept separate:
  - create_plan(): the DETERMINISTIC persist step. Takes an already-decided list of
    PlannedTaskSpec and turns it into a new, versioned MainAIPlan + MainAITask/
    MainAITaskDependency rows -- cycle-checked and validated before anything is written, fully
    unit-testable with no AI provider involved at all.
  - propose_plan_via_ai(): the ONE genuinely non-deterministic step, a single
    chat_with_fallback() call (same provider chain app/agent_orchestration.py's dispatch_task()
    already uses) asked to produce a structured JSON task breakdown, strictly parsed --
    never trusted as free text, never silently coerced into something plausible if it doesn't
    parse.

A replan (create_plan() called again for a goal that already has an active plan) supersedes
the previous plan and cancels its still-unstarted/in-flight tasks -- see create_plan()'s own
docstring for the V0.1 simplification this represents (a full fresh breakdown, not a surgical
patch), which is called out explicitly in docs/MAINAI_EXECUTION_LOOP_V0_1.md's LIMITED list.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_execution.lessons import apply_lessons_to_verification_plan
from app.models.mainai_execution import (
    TERMINAL_MAINAI_TASK_STATUSES,
    MainAIGoal,
    MainAIGoalRiskLevel,
    MainAIGoalStatus,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.providers.base import Message
from app.providers.registry import chat_with_fallback


class PlannerError(Exception):
    """Base for every planner-raised error -- never a bare ValueError/RuntimeError, so callers
    (routers, executor, tests) can catch planner failures specifically rather than any
    exception that happens to originate somewhere in this module."""


class GoalNotFoundError(PlannerError):
    def __init__(self, goal_id: uuid.UUID):
        super().__init__(f"MainAIGoal {goal_id} not found.")
        self.goal_id = goal_id


class PlanCycleError(PlannerError):
    """Raised when a proposed task dependency graph contains a cycle -- checked BEFORE any row
    is committed (see create_plan()), never discovered later as a task silently never becomes
    ready. A single-row CHECK constraint cannot express a multi-row cycle (migration 0032's
    own docstring), so this is the one place that actually enforces the DAG requirement."""

    def __init__(self, cycle_description: str):
        super().__init__(f"Task dependency graph contains a cycle: {cycle_description}")


class PlanValidationError(PlannerError):
    """Raised for a structurally invalid plan -- an out-of-range depends_on index, an unknown
    task_type, or (from propose_plan_via_ai()) a model response that isn't valid JSON matching
    the expected shape. Never silently coerced into something plausible."""


# The task_type vocabulary the executor (app/mainai_execution/executor.py) actually knows how
# to dispatch. A plan proposing an unknown task_type is rejected at PLAN time -- the same
# fail-closed principle app/mainai_runtime_contract.py's CAPABILITY_MANIFEST already
# establishes for mainai_jobs job_types, applied one layer up.
KNOWN_TASK_TYPES = frozenset({"read_only_audit", "repo_edit", "run_tests", "open_pr"})


@dataclass
class PlannedTaskSpec:
    """One task in a not-yet-persisted plan. `depends_on` holds INDICES into the same list
    (0-based) -- resolved into real MainAITask.id foreign keys only once every task in the
    plan has been inserted (see create_plan()), since a task can depend on a sibling that has
    no database id yet at spec time."""

    description: str
    task_type: str
    depends_on: list[int] = field(default_factory=list)
    risk_level: MainAIGoalRiskLevel = MainAIGoalRiskLevel.low
    approval_required: bool = False
    verification_plan: list[dict] = field(default_factory=list)
    priority: int = 0
    max_attempts: int = 3


def create_goal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    title: str,
    original_instruction: str,
    created_by: str,
    risk_level: MainAIGoalRiskLevel = MainAIGoalRiskLevel.low,
    approval_policy: str = "standard_repo_work",
) -> MainAIGoal:
    """Creates one durable goal in `pending` status -- planning has not started yet (see
    create_plan(), which transitions it to `running` once a plan actually exists)."""
    goal = MainAIGoal(
        owner_id=owner_id,
        title=title,
        original_instruction=original_instruction,
        status=MainAIGoalStatus.pending,
        risk_level=risk_level,
        approval_policy=approval_policy,
        created_by=created_by,
    )
    db.add(goal)
    db.flush()
    return goal


def get_goal(db: Session, goal_id: uuid.UUID) -> MainAIGoal:
    goal = db.get(MainAIGoal, goal_id)
    if goal is None:
        raise GoalNotFoundError(goal_id)
    return goal


def _detect_cycle(task_count: int, edges: list[tuple[int, int]]) -> list[int] | None:
    """Classic three-color DFS cycle detection over nodes 0..task_count-1 and `edges` as
    (task_index, depends_on_index) pairs. Returns the cycle's node sequence if one exists,
    else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * task_count
    adjacency: list[list[int]] = [[] for _ in range(task_count)]
    for task_idx, dep_idx in edges:
        adjacency[task_idx].append(dep_idx)

    path: list[int] = []

    def visit(node: int) -> list[int] | None:
        color[node] = GRAY
        path.append(node)
        for neighbor in adjacency[node]:
            if color[neighbor] == GRAY:
                cycle_start = path.index(neighbor)
                return path[cycle_start:] + [neighbor]
            if color[neighbor] == WHITE:
                found = visit(neighbor)
                if found is not None:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for node in range(task_count):
        if color[node] == WHITE:
            found = visit(node)
            if found is not None:
                return found
    return None


def create_plan(
    db: Session,
    *,
    goal: MainAIGoal,
    rationale: str,
    tasks: list[PlannedTaskSpec],
    created_by: str,
) -> MainAIPlan:
    """The deterministic persist step -- see this module's own docstring for the split from
    propose_plan_via_ai(). Validates the WHOLE proposed plan (unknown task_type, out-of-range
    depends_on, dependency cycle) before writing a single row -- a bad plan never partially
    lands.

    A replan (goal already has an active plan) supersedes it and cancels every one of its
    tasks that hasn't reached a terminal state yet -- a full fresh breakdown, not a surgical
    patch (V0.1 simplification, see docs/MAINAI_EXECUTION_LOOP_V0_1.md's LIMITED list). The
    superseded plan and its already-terminal tasks/events are never edited or deleted.

    Ends by calling recompute_task_readiness() so dependency-free tasks are immediately
    `ready` rather than needing a separate caller round-trip."""
    if not tasks:
        raise PlanValidationError("A plan must contain at least one task.")

    for spec in tasks:
        if spec.task_type not in KNOWN_TASK_TYPES:
            raise PlanValidationError(f"Unknown task_type '{spec.task_type}' -- the executor has no handler for it.")
        for dep_idx in spec.depends_on:
            if not (0 <= dep_idx < len(tasks)):
                raise PlanValidationError(f"depends_on index {dep_idx} is out of range for a {len(tasks)}-task plan.")

    edges = [(i, dep_idx) for i, spec in enumerate(tasks) for dep_idx in spec.depends_on]
    cycle = _detect_cycle(len(tasks), edges)
    if cycle is not None:
        raise PlanCycleError(" -> ".join(str(i) for i in cycle))

    previous_active = db.execute(
        select(MainAIPlan).where(MainAIPlan.goal_id == goal.id, MainAIPlan.status == MainAIPlanStatus.active)
    ).scalar_one_or_none()

    next_version = (goal.current_plan_version or 0) + 1

    if previous_active is not None:
        previous_active.status = MainAIPlanStatus.superseded
        stale_tasks = (
            db.execute(
                select(MainAITask).where(
                    MainAITask.plan_id == previous_active.id,
                    MainAITask.status.notin_(TERMINAL_MAINAI_TASK_STATUSES),
                )
            )
            .scalars()
            .all()
        )
        for stale_task in stale_tasks:
            stale_task.status = MainAITaskStatus.cancelled
            stale_task.completed_at = datetime.utcnow()
            stale_task.blocker_reason = f"Superseded by plan version {next_version}."
            db.add(
                MainAITaskEvent(
                    task_id=stale_task.id,
                    owner_id=stale_task.owner_id,
                    event_type=MainAITaskEventType.cancelled,
                    detail={"reason": "superseded_by_replan", "new_plan_version": next_version},
                )
            )

    plan = MainAIPlan(
        goal_id=goal.id,
        owner_id=goal.owner_id,
        version=next_version,
        status=MainAIPlanStatus.active,
        rationale=rationale,
        created_by=created_by,
    )
    db.add(plan)
    db.flush()

    persisted: list[MainAITask] = []
    applied_lessons_by_task: list[list[uuid.UUID]] = []
    for spec in tasks:
        # Engineering-lesson influence (app/mainai_execution/lessons.py): a previously recorded
        # lesson tagged for this task_type can add a targeted_tests step the spec itself didn't
        # already have -- real influence on what gets verified, not a loose suggestion. Only
        # ever ADDS to verification_plan, never removes a step the planner/founder specified.
        verification_plan, applied_lesson_ids = apply_lessons_to_verification_plan(
            db, task_type=spec.task_type, verification_plan=spec.verification_plan
        )
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
            verification_plan=verification_plan,
            max_attempts=spec.max_attempts,
        )
        db.add(task)
        persisted.append(task)
        applied_lessons_by_task.append(applied_lesson_ids)
    db.flush()

    for i, spec in enumerate(tasks):
        for dep_idx in spec.depends_on:
            db.add(
                MainAITaskDependency(
                    task_id=persisted[i].id,
                    depends_on_task_id=persisted[dep_idx].id,
                    owner_id=goal.owner_id,
                )
            )

    for task, applied_lesson_ids in zip(persisted, applied_lessons_by_task):
        detail = {"plan_version": next_version}
        if applied_lesson_ids:
            detail["lessons_applied"] = [str(lesson_id) for lesson_id in applied_lesson_ids]
        db.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.created, detail=detail))

    goal.current_plan_version = next_version
    goal.status = MainAIGoalStatus.running
    if goal.started_at is None:
        goal.started_at = datetime.utcnow()

    db.flush()

    recompute_task_readiness(db, goal_id=goal.id)

    return plan


PLANNER_SYSTEM_PROMPT = (
    "Du är MainAI:s planerare. Du får ett mål (title + fullständig instruktion). Bryt ner det "
    "i en sekvens av konkreta, avgränsade tasks. Svara ENDAST med ett JSON-objekt på formen "
    '{"tasks": [{"description": "...", "task_type": "read_only_audit|repo_edit|run_tests|'
    'open_pr", "depends_on": [0, 1], "risk_level": "low|medium|high", "approval_required": '
    'true|false, "verification_plan": [{"kind": "targeted_tests", "target": "..."}]}]} -- '
    "inget annat, ingen markdown-kodblocksmarkering, ingen förklarande text före eller efter. "
    "depends_on är 0-baserade index in i samma tasks-lista. Håll planen minimal: bara de "
    "tasks som faktiskt krävs för målet, aldrig spekulativa extra steg. En task som ändrar "
    "kod (repo_edit) eller öppnar en PR (open_pr) ska nästan alltid ha approval_required=false "
    "(godkännande krävs bara för merge/deploy, inte för att föreslå en ändring som PR) -- men "
    "en task märkt risk_level=high ska nästan alltid ha approval_required=true."
)


async def propose_plan_via_ai(db: Session, *, goal: MainAIGoal) -> tuple[list[PlannedTaskSpec], str, str]:
    """The one genuinely non-deterministic planning step -- a single chat_with_fallback() call
    (same provider chain app/agent_orchestration.py's dispatch_task() already uses) asked to
    produce a structured JSON task breakdown for `goal`. Returns (tasks, provider, model) so
    the caller can record exact execution attribution, same as every other AI-backed call in
    this codebase.

    Raises PlanValidationError if the response isn't valid JSON matching the expected shape --
    never falls back to guessing a plan from free text, and never itself writes anything to
    the database (see create_plan() for the separate, deterministic persist step this feeds)."""
    user_prompt = f"## Mål\n{goal.title}\n\n## Fullständig instruktion\n{goal.original_instruction}"
    messages = [Message(role="system", content=PLANNER_SYSTEM_PROMPT), Message(role="user", content=user_prompt)]
    result, _attempted = await chat_with_fallback(db, messages)

    raw = result.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[len("json") :]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"Planner response was not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("tasks"), list):
        raise PlanValidationError("Planner response JSON must be an object with a 'tasks' array.")

    specs: list[PlannedTaskSpec] = []
    for i, raw_task in enumerate(parsed["tasks"]):
        if not isinstance(raw_task, dict):
            raise PlanValidationError(f"tasks[{i}] must be a JSON object.")
        try:
            description = str(raw_task["description"])
            task_type = str(raw_task["task_type"])
        except KeyError as exc:
            raise PlanValidationError(f"tasks[{i}] is missing required field {exc}.") from exc

        depends_on = raw_task.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(d, int) for d in depends_on):
            raise PlanValidationError(f"tasks[{i}].depends_on must be a list of integers.")

        risk_raw = raw_task.get("risk_level", "low")
        try:
            risk_level = MainAIGoalRiskLevel(risk_raw)
        except ValueError as exc:
            raise PlanValidationError(f"tasks[{i}].risk_level '{risk_raw}' is not one of low/medium/high.") from exc

        verification_plan = raw_task.get("verification_plan", [])
        if not isinstance(verification_plan, list):
            raise PlanValidationError(f"tasks[{i}].verification_plan must be a list.")

        specs.append(
            PlannedTaskSpec(
                description=description,
                task_type=task_type,
                depends_on=depends_on,
                risk_level=risk_level,
                approval_required=bool(raw_task.get("approval_required", False)),
                verification_plan=verification_plan,
                priority=int(raw_task.get("priority", 0)),
            )
        )

    if not specs:
        raise PlanValidationError("Planner response's 'tasks' array must not be empty.")

    return specs, result.provider, result.model
