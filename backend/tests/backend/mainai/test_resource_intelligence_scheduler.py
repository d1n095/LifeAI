"""MainAI Resource Intelligence Part 2 -- `app.resource_intelligence.scheduler` -- proves
`next_best_resource_allocation()` composes `app.agent_coordination.runtime_view.
all_agents_runtime_snapshot()` with real Part 1 telemetry + Part 2 `decision.
propose_resource_action()`/`efficiency_profile.agent_efficiency_profile()` calls to produce a
ranked, advisory view -- against a real Postgres database, never mocked -- and the structural
"never mutates" proof (source-regex, matching `app.mainai_executive.judgment`'s own technique;
this module DOES take `db`, unlike the pure `decision.py`, but must never call a mutating
function through it).

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §1.7 for the architecture this
module implements."""

from __future__ import annotations

import ast
import inspect
import re
import uuid
from datetime import datetime, timedelta

import pytest

from app.agent_coordination.execution_control import start_execution_tracking
from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.resource_intelligence.scheduler import next_best_resource_allocation
from app.resource_intelligence.telemetry import record_telemetry_sample
from app.resource_intelligence.types import ContextLifecycleAction


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Resource intelligence scheduler test."):
    goal = create_goal(db, owner_id=owner_id, title="RI scheduler test", original_instruction=instruction, created_by="founder", approval_policy="standard_repo_work")
    plan = create_plan(db, goal=goal, rationale="ri scheduler test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, task


def _agent(db, key="agent", concurrency_limit=3):
    return register_agent(
        db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli",
        execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=concurrency_limit,
    )


def _new_assignment(db, *, owner_id, goal, task, agent):
    # task_id is deliberately NOT passed through here (always None), even though a real `task`
    # is available -- `_canonical_work_key()` (app.agent_coordination.service) collapses to
    # `(goal_id, task_id, role)` alone whenever task_id IS set, ignoring repository_identity
    # entirely; several agents sharing the same goal/task/role in these tests would then
    # collide on the real, correct `is_duplicate_canonical_work()` guard. With task_id=None the
    # key falls back to `(goal_id, repository_identity, role, allowed_paths)`, which the
    # agent-keyed `repository_identity` below makes unique per assignment.
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=agent.id,
        role="builder", read_write_mode="read_write", repository_identity=f"lifeai-{agent.agent_key}",
        allowed_paths=["backend/app/resource_intelligence/**"], requested_by="test",
    )


def _start_running_with_telemetry(db, *, owner_id, assignment, samples):
    """`samples` is a list of (context_used_tokens, context_window_tokens, seconds_ago) --
    each becomes one real telemetry sample with a deterministic, known sampled_at, mirroring
    Part 1's own `test_estimated_time_to_context_limit_projects_from_real_burn_rate` technique
    for a deterministic elapsed window instead of relying on real wall-clock speed."""

    transition_status(db, assignment=assignment, new_status="ready")
    transition_status(db, assignment=assignment, new_status="running")
    attempt_id = uuid.uuid4()
    start_execution_tracking(db, assignment=assignment, adapter_key="fake-cli", attempt_id=attempt_id)
    rows = []
    for used, window, seconds_ago in samples:
        rows.append(record_telemetry_sample(db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=used, context_window_tokens=window))
    db.flush()
    for row, (_, _, seconds_ago) in zip(rows, samples):
        row.sampled_at = datetime.utcnow() - timedelta(seconds=seconds_ago)
    db.flush()
    return attempt_id


# ============================================================================ composition / filtering


def test_idle_assignment_is_excluded_running_assignment_is_included(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)

    idle_assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    # left in "planned" -- never transitioned, so runtime_status is IDLE.

    running_agent = _agent(superuser_db)
    running_assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=running_agent)
    _start_running_with_telemetry(superuser_db, owner_id=owner_id, assignment=running_assignment, samples=[(1000, 200000, 1000), (1010, 200000, 0)])
    superuser_db.commit()

    rows = next_best_resource_allocation(superuser_db, owner_id=owner_id)
    assignment_ids = {row["assignment_id"] for row in rows}
    assert str(running_assignment.id) in assignment_ids
    assert str(idle_assignment.id) not in assignment_ids


def test_acute_context_assignment_outranks_healthy_assignment(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)

    critical_agent = _agent(superuser_db)
    critical_assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=critical_agent)
    # rate = 6000 tokens / 100s = 60 tokens/s; remaining = 100000-96000=4000 -> eta ~= 66.7s
    # (well under the 300s low-time-to-limit bar); utilization = 96% (>= the 95% critical bar).
    _start_running_with_telemetry(
        superuser_db, owner_id=owner_id, assignment=critical_assignment,
        samples=[(90000, 100000, 100), (96000, 100000, 0)],
    )

    healthy_agent = _agent(superuser_db)
    healthy_assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=healthy_agent)
    # rate = 10 tokens / 1000s = 0.01 tokens/s; remaining = 198990 -> eta far beyond any bar;
    # utilization = 0.505%, nowhere near any threshold.
    _start_running_with_telemetry(
        superuser_db, owner_id=owner_id, assignment=healthy_assignment,
        samples=[(1000, 200000, 1000), (1010, 200000, 0)],
    )
    superuser_db.commit()

    rows = next_best_resource_allocation(superuser_db, owner_id=owner_id)
    by_assignment = {row["assignment_id"]: row for row in rows}

    critical_row = by_assignment[str(critical_assignment.id)]
    healthy_row = by_assignment[str(healthy_assignment.id)]

    assert critical_row["recommendation"]["action"] == ContextLifecycleAction.RESET_SESSION.value
    assert healthy_row["recommendation"]["action"] == ContextLifecycleAction.CONTINUE_CURRENT_SESSION.value
    assert critical_row["priority_score"] > healthy_row["priority_score"]

    # Sorted descending overall -- the critical one really is first.
    scores = [row["priority_score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
    assert rows[0]["assignment_id"] == str(critical_assignment.id)


def test_wip_at_limit_is_reflected_and_biases_toward_defer(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db, concurrency_limit=1)
    assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    # Running, but with NO telemetry at all -- context/time-to-limit are genuinely unknown;
    # wip_at_limit must still fire (it does not depend on context data being known).
    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()

    rows = next_best_resource_allocation(superuser_db, owner_id=owner_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["wip_at_limit"] is True
    assert row["recommendation"]["action"] == ContextLifecycleAction.DEFER.value
    assert row["recommendation"]["signals"]["wip_at_limit"] is True
    assert row["recommendation"]["authorized"] is False


# ============================================================================ structural "never mutates" proof


def test_scheduler_never_calls_a_mutating_function():
    """Structural: `next_best_resource_allocation()` DOES take a `db: Session` (it is the
    composition layer, not the pure core), but the module's own source must never contain
    `db.add(`/`db.commit(` (it only ever SELECTs, via the functions it composes with, all of
    which are already independently proven read-only), nor call any of the real
    authority-granting/mutating functions this whole program forbids this package from
    calling. Matches `app.mainai_executive.judgment`'s own source-regex structural-proof
    technique, adapted for a module that legitimately touches the database (read-only)."""

    import app.resource_intelligence.scheduler as module

    source = inspect.getsource(module)
    assert not re.search(r"\bdb\.add\(", source)
    assert not re.search(r"\bdb\.commit\(", source)
    assert not re.search(r"\bUPDATE \w", source)
    assert not re.search(r"\bDELETE FROM\b", source)
    assert not re.search(r"\bINSERT INTO\b", source)

    # AST-based (not a plain substring/regex search) so this module's OWN prose -- which
    # legitimately discusses "create_work_assignment()" etc in English, e.g. in this very
    # module's docstring -- cannot produce a false failure. Only real ast.Call nodes count.
    forbidden_calls = {
        "create_work_assignment", "authorize_execution_scope", "transition_status",
        "reserve_provider_spend_call", "settle_provider_spend_call", "record_telemetry_sample",
        "save_agent_session_checkpoint", "record_capability_observation",
        "dismiss_work_candidate", "supersede_work_candidate", "acquire_lease", "release_lease",
    }
    tree = ast.parse(source)
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute):
                called_names.add(func.attr)
    hit = forbidden_calls & called_names
    assert not hit, f"scheduler.py must never call any of {hit}"

    sig = inspect.signature(next_best_resource_allocation)
    assert "db" in sig.parameters  # the composition layer -- unlike decision.py, this is expected
