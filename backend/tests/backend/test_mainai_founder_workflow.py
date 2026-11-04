"""Founder workflow: status vocabulary, deps, independent work, no false GO/approvals."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text as sa_text

from app.bootstrap import bootstrap_founder_user
from app.founder import FOUNDER_USER_ID
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.assignment_guard import CircularDependencyError, assert_no_circular_dependency
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.mainai_founder_ops.status import classify_mainai_task
from app.mainai_founder_ops.workflow import detect_dependency_cycles, independent_ready_work
from app.models.mainai_execution import (
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var
from app.workforce import submit_delegation_request
from app.workforce.broker import DelegationBrokerError
from app.workforce.registry import register_workforce_agent


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _two_task_plan(db, owner_id, *, depends_on_second=False):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="workflow goal",
        original_instruction="keep going independently",
        created_by="test",
    )
    tasks = [
        PlannedTaskSpec(description="independent ready sibling", task_type="read_only_audit"),
        PlannedTaskSpec(
            description="waits on sibling or CI",
            task_type="read_only_audit",
            depends_on=[0] if depends_on_second else [],
        ),
    ]
    create_plan(db, goal=goal, rationale="two", tasks=tasks, created_by="test")
    rows = list(db.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at.asc()))
    return goal, rows


def test_detect_dependency_cycles_finds_two_node_loop():
    a, b = uuid.uuid4(), uuid.uuid4()
    cycles = detect_dependency_cycles([(a, b), (b, a)])
    assert cycles
    ids = {node for cycle in cycles for node in cycle}
    assert str(a) in ids and str(b) in ids


def test_independent_ready_work_does_not_mark_running():
    ready = SimpleNamespace(
        id=uuid.uuid4(),
        goal_id=uuid.uuid4(),
        status=MainAITaskStatus.ready,
        description="can proceed",
    )
    waiting = SimpleNamespace(
        id=uuid.uuid4(),
        goal_id=ready.goal_id,
        status=MainAITaskStatus.waiting_ci,
        description="ci pending",
    )
    items = independent_ready_work([ready, waiting])
    assert len(items) == 1
    assert items[0]["task_id"] == str(ready.id)
    assert items[0]["running"] is False
    assert items[0]["invented_activity"] is False
    assert items[0]["founder_status"] == "queued"


def test_independent_ready_is_empty_when_no_waiting_sibling():
    ready_a = SimpleNamespace(id=uuid.uuid4(), goal_id=uuid.uuid4(), status=MainAITaskStatus.ready, description="a")
    ready_b = SimpleNamespace(id=uuid.uuid4(), goal_id=ready_a.goal_id, status=MainAITaskStatus.ready, description="b")
    assert independent_ready_work([ready_a, ready_b]) == []


def test_snapshot_surfaces_independent_ready_and_keeps_context(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    goal, rows = _two_task_plan(db_session, FOUNDER_USER_ID, depends_on_second=True)
    db_session.commit()
    ready = next(t for t in rows if t.status == MainAITaskStatus.ready)
    waiting = next(t for t in rows if t.status == MainAITaskStatus.pending)
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok"})
    assert snap["first_cycle_go"] is False
    assert snap["fabricated_progress"] is False
    ids = {item["task_id"] for item in snap["independent_ready_work"]}
    assert str(ready.id) in ids
    assert str(waiting.id) not in ids
    matching = next(item for item in snap["work_items"] if item.get("task_id") == str(ready.id))
    assert matching["description"] == "independent ready sibling"
    assert matching["goal_id"] == str(goal.id)
    assert matching["founder_status"] in {"queued", "assigned"}
    assert matching["founder_status"] != "running"
    kinds = {action["kind"] for action in snap["next_actions"]}
    assert "continue_independent_ready" in kinds
    continue_action = next(a for a in snap["next_actions"] if a["kind"] == "continue_independent_ready")
    assert continue_action["task_id"] == str(ready.id)
    assert continue_action["invented_activity"] is False


def test_granted_approval_is_not_prompted_again(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    goal = create_goal(
        db_session,
        owner_id=FOUNDER_USER_ID,
        title="approval goal",
        original_instruction="needs signoff once",
        created_by="test",
    )
    create_plan(
        db_session,
        goal=goal,
        rationale="one",
        tasks=[PlannedTaskSpec(description="privileged edit", task_type="read_only_audit", approval_required=True)],
        created_by="test",
    )
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    before = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID)
    pending_ids = {str(req["id"]) for req in before["authorization_requests"] if req["source"] == "mainai_task_approval"}
    assert str(task.id) in pending_ids
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=FOUNDER_USER_ID,
            event_type=MainAITaskEventType.approval_granted,
            detail={"granted_by": "founder"},
        )
    )
    db_session.commit()
    after = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID)
    pending_after = {str(req["id"]) for req in after["authorization_requests"] if req["source"] == "mainai_task_approval"}
    assert str(task.id) not in pending_after
    assert after["first_cycle_go"] is False
    classified = classify_mainai_task(task, events=list(db_session.query(MainAITaskEvent).filter_by(task_id=task.id)))
    assert classified["approval_granted"] is True
    assert classified["founder_status"] != "running"


def test_circular_dependency_is_blocker_and_refuses_assignment(superuser_db):
    owner = User(email=f"cycle-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key="local-classifier",
        name="Local Classifier",
        role="specialist",
        agent_type="LOCAL_MODEL",
        provider_type="local",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["low_risk_classification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    goal = create_goal(superuser_db, owner_id=owner.id, title="cycle", original_instruction="loop", created_by="test")
    create_plan(
        superuser_db,
        goal=goal,
        rationale="two",
        tasks=[
            PlannedTaskSpec(description="task a", task_type="read_only_audit"),
            PlannedTaskSpec(description="task b", task_type="read_only_audit"),
        ],
        created_by="test",
    )
    rows = list(superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at.asc()))
    a, b = rows[0], rows[1]
    superuser_db.add(MainAITaskDependency(task_id=a.id, depends_on_task_id=b.id, owner_id=owner.id))
    superuser_db.add(MainAITaskDependency(task_id=b.id, depends_on_task_id=a.id, owner_id=owner.id))
    superuser_db.commit()

    with pytest.raises(CircularDependencyError, match="circular_dependency"):
        assert_no_circular_dependency(superuser_db, owner_id=owner.id, task_id=a.id)
    with pytest.raises(DelegationBrokerError, match="circular_dependency"):
        submit_delegation_request(
            superuser_db,
            owner_id=owner.id,
            goal_text="cannot assign a cycle",
            required_capability="low_risk_classification",
            task_id=a.id,
            verification_requirement="independent_verifier",
        )

    snap = build_founder_ops_snapshot(superuser_db, owner_id=owner.id, health={"status": "ok"})
    assert snap["circular_dependencies"]
    assert any(b.get("kind") == "circular_dependency" for b in snap["blockers"])
    assert snap["first_cycle_go"] is False
    kinds = {action["kind"] for action in snap["next_actions"]}
    assert "break_circular_dependency" in kinds
    agent_names = {agent["name"] for agent in snap["agents"]}
    assert "Local Classifier" in agent_names
    idle = next(agent for agent in snap["agents"] if agent["name"] == "Local Classifier")
    assert idle["running"] is False
    assert idle["current_assignment_id"] is None


def test_snapshot_never_infers_running_from_assignment_or_go(db_session):
    bootstrap_founder_user(db_session)
    _set_rls(db_session, FOUNDER_USER_ID)
    snap = build_founder_ops_snapshot(db_session, owner_id=FOUNDER_USER_ID, health={"status": "ok"})
    assert snap["first_cycle_go"] is False
    for item in snap["work_items"]:
        if item.get("source") == "workforce_assignment" and item.get("source_status") == "assigned":
            assert item["founder_status"] == "assigned"
            assert item["founder_status"] != "running"
    assert all(item.get("founder_status") != "running" or item.get("liveness") == "running" or item.get("source") in {"mainai_job", "agent_task", "workforce_assignment"} for item in snap["work_items"])
