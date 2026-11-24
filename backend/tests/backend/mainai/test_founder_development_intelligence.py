"""Founder Development Intelligence (app.agent_coordination.founder_intelligence) -- proves the
NEW read-only synthesis layer against a realistic mix of WORKING / BLOCKED / FINISHED
assignments plus a duplicate-work pair, built on the SAME real Postgres tables
test_multi_agent_work_coordination.py and test_agent_runtime_control_plane.py already exercise.
Never re-tests those modules' own conflict/lease/readiness logic -- only the new plain-language
classification, CI-evidence surfacing, duplicate-warning, and redundant-update suppression this
workstream actually added. No GitHub call, no agent dispatch, no repository write anywhere in
this file -- a simulated/proposal-only acceptance, as required."""

import uuid

import pytest

from app.agent_coordination.founder_intelligence import founder_development_summary
from app.agent_coordination.service import (
    build_agent_outcome_payload,
    create_work_assignment,
    record_assignment_execution,
    record_assignment_outcome,
    register_agent,
    transition_status,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import AgentWorkAssignment
from app.models.mainai_execution import MainAITask


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_with_tasks(db, owner_id, *, count):
    goal = create_goal(
        db, owner_id=owner_id, title="Founder dev intelligence test", original_instruction="x", created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db, goal=goal, rationale="test",
        tasks=[PlannedTaskSpec(description=f"Task {i}", task_type="repo_edit") for i in range(count)],
        created_by="founder",
    )
    db.commit()
    return goal, db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).all()


def _agent(db, key):
    return register_agent(
        db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli",
        execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3,
    )


def _assign(db, *, owner_id, goal, task, agent, **kwargs):
    defaults = {"role": "builder", "read_write_mode": "read_write", "repository_identity": "lifeai", "allowed_paths": ["backend/app/x/**"], "requested_by": "test"}
    defaults.update(kwargs)
    return create_work_assignment(db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id, **defaults)


def test_working_blocked_and_finished_classified_correctly(superuser_db, owner_id):
    """The directive's own acceptance scenario: a realistic mix of agent states, correctly
    classified in plain language from real DB state -- nothing mocked, nothing guessed."""

    goal, tasks = _goal_with_tasks(superuser_db, owner_id, count=4)
    task_running, task_dep, task_blocked, task_done = tasks

    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    claude = _agent(superuser_db, "claude-code")

    running = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_running, agent=cursor)
    transition_status(superuser_db, assignment=running, new_status="ready")
    transition_status(superuser_db, assignment=running, new_status="running")

    dependency = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_dep, agent=codex)
    blocked = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task_blocked, agent=claude,
        depends_on_assignment_ids=(dependency.id,),
    )
    superuser_db.commit()

    done = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_done, agent=cursor)
    transition_status(superuser_db, assignment=done, new_status="ready")
    transition_status(superuser_db, assignment=done, new_status="running")
    record_assignment_execution(superuser_db, assignment=done, agent=cursor)
    record_assignment_outcome(
        superuser_db, assignment=done, evidence_kind="dispatch_result",
        payload=build_agent_outcome_payload(tests_passed=True, tests_run=12, ci_conclusion="success"),
        deterministic=True,
    )
    transition_status(superuser_db, assignment=done, new_status="completed")
    superuser_db.commit()

    summaries = founder_development_summary(superuser_db, owner_id=owner_id)
    by_id = {s.assignment_id: s for s in summaries}

    assert by_id[running.id].plain_status == "Arbetar just nu"
    assert by_id[running.id].duplicate_warning is None

    assert by_id[blocked.id].plain_status == "Väntar på en annan uppgift"
    assert by_id[blocked.id].block_explanation is not None
    assert "Väntar på en annan uppgift" in by_id[blocked.id].block_explanation

    assert by_id[done.id].plain_status == "Klar"
    assert by_id[done.id].ci_conclusion == "success"


def test_duplicate_canonical_work_is_flagged(superuser_db, owner_id):
    """`create_work_assignment` already refuses to CREATE a genuine duplicate (proven by
    test_multi_agent_work_coordination.py) -- this proves the separate READ-side detector this
    workstream added catches a pair that nonetheless ended up coexisting (e.g. one created
    through a different path, or left over from before this guard existed), which is exactly
    the scenario the founder needs warned about. The second row is inserted directly via the
    ORM, deliberately bypassing create_work_assignment's own guard, to construct that state."""

    goal, (task,) = _goal_with_tasks(superuser_db, owner_id, count=1)
    agent_a = _agent(superuser_db, "cursor-agent")
    agent_b = _agent(superuser_db, "codex")

    first = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent_a)
    superuser_db.commit()

    second = AgentWorkAssignment(
        owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent_b.id,
        role=first.role, read_write_mode=first.read_write_mode, repository_identity="lifeai",
        allowed_paths=["backend/app/y/**"], canonical_work_key=first.canonical_work_key, requested_by="test",
    )
    superuser_db.add(second)
    superuser_db.commit()

    summaries = founder_development_summary(superuser_db, owner_id=owner_id)
    by_id = {s.assignment_id: s for s in summaries}

    assert by_id[first.id].duplicate_warning is not None
    assert str(second.id) in by_id[first.id].duplicate_warning
    assert by_id[second.id].duplicate_warning is not None
    assert str(first.id) in by_id[second.id].duplicate_warning


def test_redundant_update_suppression(superuser_db, owner_id):
    """A row whose signature the caller already has is omitted entirely on the next call --
    the caller sees exactly the delta. No new table is involved; the caller supplies what it
    last saw."""

    goal, (task,) = _goal_with_tasks(superuser_db, owner_id, count=1)
    agent = _agent(superuser_db, "cursor-agent")
    assignment = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    first_call = founder_development_summary(superuser_db, owner_id=owner_id)
    assert len(first_call) == 1
    signature = first_call[0].signature

    second_call = founder_development_summary(
        superuser_db, owner_id=owner_id, known_signatures={str(assignment.id): signature}
    )
    assert second_call == []

    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()

    third_call = founder_development_summary(
        superuser_db, owner_id=owner_id, known_signatures={str(assignment.id): signature}
    )
    assert len(third_call) == 1
    assert third_call[0].plain_status == "Arbetar just nu"
