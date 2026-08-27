"""MainAI Execution Loop V0.1 — planner (app/mainai_execution/planner.py) and task graph
readiness (app/mainai_execution/graph.py). See migration 0032 and
app/models/mainai_execution.py for the schema this exercises.

Covers, in order:
  A. create_goal(): durable goal creation, correct initial status.
  B. create_plan(): deterministic persist — task/dependency insertion, event recording,
     immediate readiness for dependency-free tasks.
  C. Validation: empty plan, unknown task_type, out-of-range depends_on — all rejected before
     any row is written.
  D. Cycle detection: a dependency cycle is rejected, no partial plan lands.
  E. Task graph readiness: recompute_task_readiness() promotes tasks whose dependencies just
     completed, and moves a task to `blocked` (not silently `pending` forever) when a
     dependency fails/is cancelled.
  F. next_ready_task(): priority + FIFO ordering across an owner's ready tasks.
  G. Replan: create_plan() called again for the same goal supersedes the previous plan and
     cancels its still-unstarted tasks, without touching already-completed ones or deleting
     any history.

  H. propose_plan_via_ai(): the AI-assisted breakdown step, provider faked (never a real
     key) — matching test_agent_orchestration.py's own convention for chat_with_fallback().
     Valid JSON is parsed into PlannedTaskSpec objects; invalid JSON, a missing 'tasks' key,
     and an empty tasks array are all rejected as PlanValidationError, never silently
     coerced.

  I. State-machine mutation matrix (hardening pass): direct raw-SQL writes that attempt to
     violate migration 0032's `mainai_tasks` CHECK constraints, proving those constraints are
     genuinely enforced by Postgres against a real violating write -- not just present in the
     migration SQL and trusted by convention (see _mark_terminal()'s own docstring above,
     which documents the convention every application code path already follows; this section
     proves the DB itself refuses to accept a write that breaks it, e.g. from a future bug in
     application code that forgets the convention).

Real local Postgres (RLS included)."""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import graph, lessons, planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import (
    EngineeringLessonSeverity,
    MainAIGoalStatus,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as test_mainai_jobs.py's own identical fixture — this
    module's writes to mainai_goals/mainai_plans/mainai_tasks/mainai_task_events must not
    depend on some OTHER test module having already applied
    app/rls.py's apply_mainai_execution_privileges() first."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _mark_terminal(task: MainAITask, status: MainAITaskStatus) -> None:
    """Test-side stand-in for what a real executor completion/failure path must do: DB
    migration 0032's `ck_mainai_tasks_completed_at_matches_terminal_status` CHECK requires
    completed_at to be set in the SAME statement as any terminal status transition -- a task
    can never be `completed`/`failed`/`cancelled` with a NULL completed_at, enforced at the
    database level, not just by convention."""
    task.status = status
    task.completed_at = datetime.utcnow()


def _goal(db_session, owner_id, *, title="Test goal"):
    return planner.create_goal(
        db_session,
        owner_id=owner_id,
        title=title,
        original_instruction="Do the thing, carefully.",
        created_by="test",
    )


# ---------------------------------------------------------------- A. create_goal


def test_create_goal_starts_pending_with_no_plan(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.pending
    assert goal.current_plan_version == 0
    assert goal.started_at is None
    assert goal.completed_at is None


def test_get_goal_raises_for_unknown_id(db_session, owner_id):
    with pytest.raises(planner.GoalNotFoundError):
        planner.get_goal(db_session, uuid.uuid4())


# ---------------------------------------------------------------- B. create_plan persist


def test_create_plan_persists_tasks_and_marks_dependency_free_tasks_ready(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    tasks = [
        PlannedTaskSpec(description="Read the repo", task_type="read_only_audit"),
        PlannedTaskSpec(description="Make the edit", task_type="repo_edit", depends_on=[0]),
        PlannedTaskSpec(description="Run tests", task_type="run_tests", depends_on=[1]),
    ]
    plan = planner.create_plan(db_session, goal=goal, rationale="three-step plan", tasks=tasks, created_by="test")
    db_session.commit()

    assert plan.version == 1
    assert plan.status == MainAIPlanStatus.active
    assert goal.current_plan_version == 1
    assert goal.status == MainAIGoalStatus.running
    assert goal.started_at is not None

    persisted = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).order_by(MainAITask.created_at).all()
    assert len(persisted) == 3
    # Task 0 has no dependencies -> immediately ready. Tasks 1/2 wait on an in-flight dep.
    assert persisted[0].status == MainAITaskStatus.ready
    assert persisted[1].status == MainAITaskStatus.pending
    assert persisted[2].status == MainAITaskStatus.pending

    events = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(persisted[0].id)}).all()
    event_types = {row[0] for row in events}
    assert event_types == {MainAITaskEventType.created.value, MainAITaskEventType.ready.value}


# ---------------------------------------------------------------- C. validation


def test_create_plan_rejects_empty_task_list(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    with pytest.raises(planner.PlanValidationError):
        planner.create_plan(db_session, goal=goal, rationale="empty", tasks=[], created_by="test")


def test_create_plan_rejects_unknown_task_type(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    with pytest.raises(planner.PlanValidationError):
        planner.create_plan(
            db_session,
            goal=goal,
            rationale="bad type",
            tasks=[PlannedTaskSpec(description="???", task_type="deploy_to_production")],
            created_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).count() == 0


def test_create_plan_rejects_out_of_range_depends_on(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    with pytest.raises(planner.PlanValidationError):
        planner.create_plan(
            db_session,
            goal=goal,
            rationale="bad dep",
            tasks=[PlannedTaskSpec(description="only task", task_type="read_only_audit", depends_on=[5])],
            created_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).count() == 0


@pytest.mark.parametrize("bad_target", ["../../../etc/passwd", "tests/../../secrets.py", "/etc/passwd", "..", "tests/.."])
def test_create_plan_rejects_a_path_traversing_or_absolute_targeted_tests_target(db_session, owner_id, bad_target):
    """Hardening pass finding (P1): verification_plan is AI-proposed content
    (propose_plan_via_ai()) that ultimately becomes a real `python -m pytest` subprocess
    argument (verify.py / execution_job.py) -- a hallucinated or prompt-injected plan pointing
    a `targeted_tests` step outside the repo must be rejected at PLAN time, fail-closed, same
    as an unknown task_type or an out-of-range depends_on index above."""
    goal = _goal(db_session, owner_id)
    with pytest.raises(planner.PlanValidationError):
        planner.create_plan(
            db_session,
            goal=goal,
            rationale="unsafe verification target",
            tasks=[PlannedTaskSpec(description="run tests", task_type="run_tests", verification_plan=[{"kind": "targeted_tests", "target": bad_target}])],
            created_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).count() == 0


def test_create_plan_rejects_a_malformed_verification_plan_entry(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    with pytest.raises(planner.PlanValidationError):
        planner.create_plan(
            db_session,
            goal=goal,
            rationale="malformed step",
            tasks=[PlannedTaskSpec(description="run tests", task_type="run_tests", verification_plan=[{"no_kind_field": True}])],
            created_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).count() == 0


def test_create_plan_accepts_a_safe_relative_targeted_tests_target(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="safe verification target",
        tasks=[
            PlannedTaskSpec(
                description="run tests", task_type="run_tests", verification_plan=[{"kind": "targeted_tests", "target": "tests/backend/test_something.py"}]
            )
        ],
        created_by="test",
    )
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    assert task.verification_plan == [{"kind": "targeted_tests", "target": "tests/backend/test_something.py"}]


def test_record_engineering_lesson_for_the_targeted_tests_path_traversal_fix(db_session, owner_id):
    """MAINAI V0.1 hardening pass (post-PR #57): engineering learning loop, mandatory for a
    P1 finding. `verification_plan`'s `targeted_tests.target` is AI-proposed content
    (propose_plan_via_ai() -- untrusted input) that becomes a real `python -m pytest`
    subprocess argument in TWO call sites (verify.py's verify_task(), execution_job.py's
    run_tests handler). Neither create_plan() nor either execution call site validated it --
    an absolute path or a `..`-escaping relative path would have let a hallucinated or
    prompt-injected plan point verification at an arbitrary file on the executor's own
    filesystem, which pytest would then import and execute. `_handle_repo_edit()`'s own
    AI-proposed file-WRITE path already refused `..` for exactly this class of risk
    (_parse_code_agent_response()) -- this had been missed for verification targets."""
    lesson = lessons.record_lesson(
        db_session,
        problem=(
            "A `targeted_tests` verification_plan step's `target` field (AI-proposed, via "
            "propose_plan_via_ai()) was never validated for path safety before becoming an "
            "argv element to a real `python -m pytest` subprocess call in verify.py's "
            "verify_task() and execution_job.py's run_tests handler -- an absolute path or a "
            "`..`-escaping relative path would let pytest collect and import an arbitrary file "
            "on the executor's own filesystem, running any module-level code and any `test_*` "
            "function it contains."
        ),
        root_cause=(
            "create_plan() validated task_type, depends_on indices, and cycle-freedom, but "
            "never inspected the CONTENTS of a task's verification_plan entries. The identical "
            "class of risk was already recognized and fixed for AI-proposed file WRITE paths "
            "in _handle_repo_edit()'s _parse_code_agent_response() (`if '..' in "
            "Path(f['path']).parts: raise`), but that discipline was not carried over to "
            "verification targets."
        ),
        affected_component="app.mainai_execution.planner / app.mainai_execution.verify / app.mainai_execution.execution_job",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Found by direct comparison to the codebase's own existing precedent "
            "(_parse_code_agent_response()'s '..' check for file writes) during the V0.1 "
            "hardening pass's planner/subprocess attack review -- no existing test covered "
            "targeted_tests target validation at all before this pass."
        ),
        fix=(
            "Added validate_targeted_tests_target() (app/mainai_execution/verify.py): rejects "
            "a non-string/empty target, an absolute path, or any path containing a '..' "
            "segment. Wired into THREE places for defense in depth: create_plan() (plan-creation "
            "time, fail-closed before anything is persisted), verify_task()'s own "
            "_run_targeted_tests(), and execution_job.py's _run_pytest() (the run_tests task "
            "type's own primary-work call site) -- so a target that somehow bypassed the "
            "plan-time check is still caught at both real subprocess boundaries."
        ),
        general_rule=(
            "Any AI-proposed value that becomes a filesystem path or subprocess argument must "
            "be validated for path-traversal/absolute-path safety at EVERY boundary that uses "
            "it, not just the first one -- a fix applied to one call site (file writes) does "
            "not automatically cover a structurally identical risk at a different call site "
            "(verification targets) unless someone deliberately generalizes it."
        ),
        applies_to=["run_tests", "repo_edit", "verification_plan", "targeted_tests", "planner"],
        source_type="branch_registry_pass",
        source_ref="MAINAI V0.1 hardening pass (post-PR #57) -- targeted_tests path-traversal fix",
        created_by="hardening-pass",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_planner.py::test_create_plan_rejects_a_path_traversing_or_absolute_targeted_tests_target",
    )
    db_session.commit()

    assert lesson.id is not None
    found = lessons.lookup_lessons(db_session, applies_to_any=["run_tests"])
    assert any(item.id == lesson.id for item in found)


# ---------------------------------------------------------------- D. cycle detection


def test_create_plan_rejects_a_dependency_cycle_and_writes_nothing(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    cyclic_tasks = [
        PlannedTaskSpec(description="A", task_type="read_only_audit", depends_on=[2]),
        PlannedTaskSpec(description="B", task_type="read_only_audit", depends_on=[0]),
        PlannedTaskSpec(description="C", task_type="read_only_audit", depends_on=[1]),
    ]
    with pytest.raises(planner.PlanCycleError):
        planner.create_plan(db_session, goal=goal, rationale="cyclic", tasks=cyclic_tasks, created_by="test")
    db_session.rollback()

    assert db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).count() == 0
    assert goal.current_plan_version == 0


def test_detect_cycle_pure_function_finds_and_clears_cycles():
    assert planner._detect_cycle(3, [(0, 1), (1, 2), (2, 0)]) is not None
    assert planner._detect_cycle(3, [(0, 1), (1, 2)]) is None
    assert planner._detect_cycle(1, []) is None
    # A diamond (A->B, A->C, B->D, C->D) is a valid DAG, not a cycle.
    assert planner._detect_cycle(4, [(1, 0), (2, 0), (3, 1), (3, 2)]) is None


# ---------------------------------------------------------------- E. graph readiness


def test_recompute_task_readiness_promotes_dependents_once_their_dependency_completes(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    tasks = [
        PlannedTaskSpec(description="first", task_type="read_only_audit"),
        PlannedTaskSpec(description="second", task_type="run_tests", depends_on=[0]),
    ]
    planner.create_plan(db_session, goal=goal, rationale="two-step", tasks=tasks, created_by="test")
    db_session.commit()

    first, second = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    assert first.status == MainAITaskStatus.ready
    assert second.status == MainAITaskStatus.pending

    # Simulate the executor completing the first task.
    _mark_terminal(first, MainAITaskStatus.completed)
    db_session.flush()

    newly_ready = graph.recompute_task_readiness(db_session, goal_id=goal.id)
    db_session.commit()

    assert [t.id for t in newly_ready] == [second.id]
    db_session.refresh(second)
    assert second.status == MainAITaskStatus.ready


def test_recompute_task_readiness_blocks_a_dependent_of_a_failed_task_rather_than_stalling_silently(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    tasks = [
        PlannedTaskSpec(description="first", task_type="read_only_audit"),
        PlannedTaskSpec(description="second", task_type="run_tests", depends_on=[0]),
    ]
    planner.create_plan(db_session, goal=goal, rationale="two-step", tasks=tasks, created_by="test")
    db_session.commit()

    first, second = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    _mark_terminal(first, MainAITaskStatus.failed)
    db_session.flush()

    graph.recompute_task_readiness(db_session, goal_id=goal.id)
    db_session.commit()

    db_session.refresh(second)
    assert second.status == MainAITaskStatus.blocked
    assert second.blocker_reason is not None and str(first.id) in second.blocker_reason

    events = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(second.id)}).all()
    assert MainAITaskEventType.blocked.value in {row[0] for row in events}


def test_recompute_task_readiness_is_idempotent(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single",
        tasks=[PlannedTaskSpec(description="only", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()

    # Already ready from create_plan()'s own call -- calling again must find nothing new to
    # promote and must not error or double-record events.
    newly_ready = graph.recompute_task_readiness(db_session, goal_id=goal.id)
    db_session.commit()
    assert newly_ready == []


# ---------------------------------------------------------------- F. next_ready_task


def test_next_ready_task_orders_by_priority_then_fifo(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    tasks = [
        PlannedTaskSpec(description="low priority", task_type="read_only_audit", priority=0),
        PlannedTaskSpec(description="high priority", task_type="read_only_audit", priority=10),
    ]
    planner.create_plan(db_session, goal=goal, rationale="priority test", tasks=tasks, created_by="test")
    db_session.commit()

    picked = graph.next_ready_task(db_session, owner_id=owner_id)
    assert picked is not None
    assert picked.description == "high priority"


def test_next_ready_task_returns_none_when_nothing_is_ready(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="blocked chain",
        tasks=[
            PlannedTaskSpec(description="a", task_type="read_only_audit"),
            PlannedTaskSpec(description="b", task_type="read_only_audit", depends_on=[0]),
        ],
        created_by="test",
    )
    db_session.commit()
    first = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.description == "a").one()
    first.status = MainAITaskStatus.running  # claimed but not yet completed -- "b" stays pending, not ready
    db_session.commit()

    picked = graph.next_ready_task(db_session, owner_id=owner_id)
    assert picked is None


# ---------------------------------------------------------------- G. replan


def test_create_plan_called_again_supersedes_previous_plan_and_cancels_its_unstarted_tasks(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    plan_1 = planner.create_plan(
        db_session,
        goal=goal,
        rationale="v1",
        tasks=[
            PlannedTaskSpec(description="v1-a", task_type="read_only_audit"),
            PlannedTaskSpec(description="v1-b", task_type="run_tests", depends_on=[0]),
        ],
        created_by="test",
    )
    db_session.commit()

    v1_a = db_session.query(MainAITask).filter(MainAITask.plan_id == plan_1.id, MainAITask.description == "v1-a").one()
    _mark_terminal(v1_a, MainAITaskStatus.completed)  # this one finished before the replan
    db_session.commit()

    plan_2 = planner.create_plan(
        db_session,
        goal=goal,
        rationale="v2 -- scope changed",
        tasks=[PlannedTaskSpec(description="v2-only", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()

    db_session.refresh(plan_1)
    assert plan_1.status == MainAIPlanStatus.superseded
    assert plan_2.status == MainAIPlanStatus.active
    assert plan_2.version == 2
    assert goal.current_plan_version == 2

    db_session.refresh(v1_a)
    assert v1_a.status == MainAITaskStatus.completed  # untouched -- already terminal before the replan

    v1_b = db_session.query(MainAITask).filter(MainAITask.plan_id == plan_1.id, MainAITask.description == "v1-b").one()
    assert v1_b.status == MainAITaskStatus.cancelled
    assert v1_b.blocker_reason is not None and "Superseded" in v1_b.blocker_reason

    v2_only = db_session.query(MainAITask).filter(MainAITask.plan_id == plan_2.id).one()
    assert v2_only.status == MainAITaskStatus.ready

    # History is never deleted: both plans and all four tasks (v1-a, v1-b, v2-only, and any
    # events) still exist in the table.
    assert db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).count() == 3


# ---------------------------------------------------------------- H. propose_plan_via_ai


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


_VALID_PLAN_JSON = """{
  "tasks": [
    {"description": "Read the relevant files", "task_type": "read_only_audit", "depends_on": [], "risk_level": "low"},
    {"description": "Apply the fix", "task_type": "repo_edit", "depends_on": [0], "risk_level": "medium", "approval_required": false},
    {"description": "Run the targeted tests", "task_type": "run_tests", "depends_on": [1], "verification_plan": [{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}]}
  ]
}"""


@pytest.mark.asyncio
async def test_propose_plan_via_ai_parses_a_valid_response_into_planned_task_specs(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat(_VALID_PLAN_JSON))
    goal = _goal(db_session, owner_id)

    specs, provider, model = await planner.propose_plan_via_ai(db_session, goal=goal)

    assert provider == "openai"
    assert len(specs) == 3
    assert specs[0].task_type == "read_only_audit"
    assert specs[1].depends_on == [0]
    assert specs[2].verification_plan == [{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}]

    # The result is directly usable by create_plan() -- proving the two steps' contract
    # actually lines up, not just that each parses in isolation.
    plan = planner.create_plan(db_session, goal=goal, rationale="AI-proposed", tasks=specs, created_by="test")
    db_session.commit()
    assert plan.version == 1


@pytest.mark.asyncio
async def test_propose_plan_via_ai_never_egress_marked_instruction_is_denied_before_the_provider_is_ever_called(
    db_session, owner_id, monkeypatch
):
    """Life Vault / External-AI Egress Control (docs/LIFE_VAULT_EGRESS_CONTROL.md, V4):
    propose_plan_via_ai() now routes through chat_with_fallback(owner_id=goal.owner_id).
    goal.owner_id was already available in this function's own scope -- no identity-
    propagation gap here, unlike lesson_conflicts.py/agent_orchestration.py's genuinely
    ownerless models (see the threat model doc's V4 row)."""
    from app.egress_policy import EgressDeniedError

    chat_calls: list[str] = []

    async def _tracking_chat(self, messages, model, **kwargs):
        chat_calls.append(self.name)
        return ChatResult(content=_VALID_PLAN_JSON, provider=self.name, model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _tracking_chat)
    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Marked goal",
        original_instruction="NEVER_EGRESS: hemlig instruktion som aldrig far lamna processen.",
        created_by="test",
    )

    with pytest.raises(EgressDeniedError):
        await planner.propose_plan_via_ai(db_session, goal=goal)

    assert chat_calls == []  # the marked instruction never reached the chat provider


@pytest.mark.asyncio
async def test_propose_plan_via_ai_strips_markdown_code_fences(db_session, owner_id, monkeypatch):
    fenced = "```json\n" + _VALID_PLAN_JSON + "\n```"
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat(fenced))
    goal = _goal(db_session, owner_id)

    specs, _provider, _model = await planner.propose_plan_via_ai(db_session, goal=goal)
    assert len(specs) == 3


@pytest.mark.asyncio
async def test_propose_plan_via_ai_rejects_invalid_json(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Visst, här är planen: steg 1, steg 2..."))
    goal = _goal(db_session, owner_id)

    with pytest.raises(planner.PlanValidationError):
        await planner.propose_plan_via_ai(db_session, goal=goal)


@pytest.mark.asyncio
async def test_propose_plan_via_ai_rejects_a_missing_tasks_key(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat('{"plan": "not the expected shape"}'))
    goal = _goal(db_session, owner_id)

    with pytest.raises(planner.PlanValidationError):
        await planner.propose_plan_via_ai(db_session, goal=goal)


@pytest.mark.asyncio
async def test_propose_plan_via_ai_rejects_an_empty_tasks_array(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat('{"tasks": []}'))
    goal = _goal(db_session, owner_id)

    with pytest.raises(planner.PlanValidationError):
        await planner.propose_plan_via_ai(db_session, goal=goal)


@pytest.mark.asyncio
async def test_propose_plan_via_ai_rejects_an_unknown_risk_level(db_session, owner_id, monkeypatch):
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat('{"tasks": [{"description": "x", "task_type": "read_only_audit", "risk_level": "catastrophic"}]}'),
    )
    goal = _goal(db_session, owner_id)

    with pytest.raises(planner.PlanValidationError):
        await planner.propose_plan_via_ai(db_session, goal=goal)


# ---------------------------------------------------------------- I. state-machine mutation matrix
#
# Hardening pass: proving migration 0032's mainai_tasks CHECK constraints genuinely reject a
# violating write at the database level, not just document a convention application code
# happens to follow. Each test attempts the exact mutation the constraint exists to forbid,
# via raw SQL bypassing every ORM-level guard, and expects Postgres itself to refuse it.


def _one_task(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="t", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def test_ck_completed_at_matches_terminal_status_rejects_completed_without_completed_at(db_session, owner_id):
    from sqlalchemy.exc import IntegrityError

    task = _one_task(db_session, owner_id)

    with pytest.raises(IntegrityError):
        db_session.execute(sa_text("UPDATE mainai_tasks SET status = 'completed' WHERE id = :id"), {"id": str(task.id)})
    db_session.rollback()


def test_ck_completed_at_matches_terminal_status_rejects_completed_at_set_on_a_non_terminal_status(db_session, owner_id):
    """The reverse direction of the same constraint: a still-`ready` task can never carry a
    non-NULL completed_at either -- otherwise a report/UI reading `completed_at IS NOT NULL`
    as a proxy for "done" could be fooled by a task that never actually reached a terminal
    status."""
    from sqlalchemy.exc import IntegrityError

    task = _one_task(db_session, owner_id)
    assert task.status == MainAITaskStatus.ready

    with pytest.raises(IntegrityError):
        db_session.execute(sa_text("UPDATE mainai_tasks SET completed_at = now() WHERE id = :id"), {"id": str(task.id)})
    db_session.rollback()


def test_ck_attempts_within_budget_rejects_attempts_exceeding_max_attempts(db_session, owner_id):
    from sqlalchemy.exc import IntegrityError

    task = _one_task(db_session, owner_id)
    assert task.max_attempts == 3

    with pytest.raises(IntegrityError):
        db_session.execute(sa_text("UPDATE mainai_tasks SET attempts = max_attempts + 1 WHERE id = :id"), {"id": str(task.id)})
    db_session.rollback()
