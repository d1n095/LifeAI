"""PostgreSQL adversarial coverage for canonical-work and terminal-state fencing."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.agent_coordination.service import (
    DuplicateWorkError,
    InvalidTransitionError,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import (
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    WorkAssignmentReadWriteMode,
    WorkAssignmentRole,
    WorkAssignmentStatus,
)
from app.models.mainai_execution import MainAITask


def _foundation(db, owner_id):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="Concurrency fencing",
        original_instruction="Prove database coordination fencing.",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db,
        goal=goal,
        rationale="adversarial concurrency test",
        tasks=[
            PlannedTaskSpec(
                description="One canonical task",
                task_type="repo_edit",
                target_files=["backend/app/agent_coordination/service.py"],
            )
        ],
        created_by="founder",
    )
    agent = register_agent(
        db,
        agent_key=f"concurrency-{uuid.uuid4()}",
        display_name="Concurrency test agent",
        adapter_kind="cli",
        supports_read=True,
        supports_write=True,
    )
    db.commit()
    task = db.execute(select(MainAITask).where(MainAITask.plan_id == plan.id)).scalar_one()
    return goal.id, task.id, agent.id


def _create(session, *, owner_id, goal_id, task_id, agent_id):
    return create_work_assignment(
        session,
        owner_id=owner_id,
        goal_id=goal_id,
        task_id=task_id,
        agent_id=agent_id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="d1n095/LifeAI",
        allowed_paths=["backend/app/agent_coordination/**"],
        requested_by="concurrency-test",
    )


def test_concurrent_service_creation_has_one_canonical_winner(
    superuser_db, make_verified_user
):
    from app.db import migration_engine

    owner, _password = make_verified_user()
    goal_id, task_id, agent_id = _foundation(superuser_db, owner.id)
    sessions = sessionmaker(bind=migration_engine)
    barrier = Barrier(2)

    def contender():
        with sessions() as db:
            barrier.wait(timeout=10)
            try:
                assignment = _create(
                    db,
                    owner_id=owner.id,
                    goal_id=goal_id,
                    task_id=task_id,
                    agent_id=agent_id,
                )
                db.commit()
                return "created", assignment.id
            except DuplicateWorkError:
                db.rollback()
                return "duplicate", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: contender(), range(2)))

    assert sorted(outcome for outcome, _ in outcomes) == ["created", "duplicate"]
    rows = superuser_db.execute(
        select(AgentWorkAssignment).where(
            AgentWorkAssignment.owner_id == owner.id,
            AgentWorkAssignment.goal_id == goal_id,
        )
    ).scalars().all()
    assert len(rows) == 1


def test_database_trigger_blocks_concurrent_direct_insert_bypass(
    superuser_db, make_verified_user
):
    from app.db import migration_engine

    owner, _password = make_verified_user()
    goal_id, task_id, agent_id = _foundation(superuser_db, owner.id)
    sessions = sessionmaker(bind=migration_engine)
    barrier = Barrier(2)
    canonical_key = f"direct-{uuid.uuid4()}"

    def bypass_service():
        with sessions() as db:
            row = AgentWorkAssignment(
                owner_id=owner.id,
                goal_id=goal_id,
                task_id=task_id,
                agent_id=agent_id,
                role=WorkAssignmentRole.builder,
                read_write_mode=WorkAssignmentReadWriteMode.read_write,
                repository_identity="d1n095/LifeAI",
                allowed_paths=["backend/**"],
                status=WorkAssignmentStatus.ready,
                canonical_work_key=canonical_key,
                requested_by="direct-sql-adversary",
            )
            db.add(row)
            barrier.wait(timeout=10)
            try:
                db.commit()
                return "created"
            except IntegrityError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: bypass_service(), range(2)))

    assert sorted(outcomes) == ["created", "rejected"]
    rows = superuser_db.execute(
        select(AgentWorkAssignment).where(
            AgentWorkAssignment.owner_id == owner.id,
            AgentWorkAssignment.canonical_work_key == canonical_key,
        )
    ).scalars().all()
    assert len(rows) == 1


def test_conflicting_terminal_transitions_cannot_both_commit(
    superuser_db, make_verified_user
):
    from app.db import migration_engine

    owner, _password = make_verified_user()
    goal_id, task_id, agent_id = _foundation(superuser_db, owner.id)
    assignment = _create(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal_id,
        task_id=task_id,
        agent_id=agent_id,
    )
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()
    assignment_id = assignment.id

    sessions = sessionmaker(bind=migration_engine)
    barrier = Barrier(2)

    def finish(target):
        with sessions() as db:
            stale = db.get(AgentWorkAssignment, assignment_id)
            assert stale.status == WorkAssignmentStatus.running
            barrier.wait(timeout=10)
            try:
                transition_status(db, assignment=stale, new_status=target)
                db.commit()
                return target
            except InvalidTransitionError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, ("completed", "failed")))

    assert outcomes.count("rejected") == 1
    winner = next(outcome for outcome in outcomes if outcome != "rejected")
    superuser_db.expire_all()
    final = superuser_db.get(AgentWorkAssignment, assignment_id)
    assert final.status.value == winner
    events = superuser_db.execute(
        select(AgentWorkAssignmentEvent).where(
            AgentWorkAssignmentEvent.assignment_id == assignment_id,
            AgentWorkAssignmentEvent.event_type
            == AgentWorkAssignmentEventType.status_changed,
        )
    ).scalars().all()
    terminal_events = [
        event for event in events if event.detail.get("to") in {"completed", "failed"}
    ]
    assert [event.detail["to"] for event in terminal_events] == [winner]


def test_database_rejects_direct_conflicting_terminal_updates(
    superuser_db, make_verified_user
):
    from app.db import migration_engine

    owner, _password = make_verified_user()
    goal_id, task_id, agent_id = _foundation(superuser_db, owner.id)
    assignment = _create(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal_id,
        task_id=task_id,
        agent_id=agent_id,
    )
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()
    assignment_id = assignment.id
    sessions = sessionmaker(bind=migration_engine)
    barrier = Barrier(2)

    def direct_update(target):
        with sessions() as db:
            barrier.wait(timeout=10)
            try:
                db.execute(
                    text(
                        "UPDATE agent_work_assignments SET status = :target "
                        "WHERE id = :assignment_id"
                    ),
                    {"target": target, "assignment_id": assignment_id},
                )
                db.commit()
                return target
            except IntegrityError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(direct_update, ("completed", "failed")))

    assert outcomes.count("rejected") == 1
    winner = next(outcome for outcome in outcomes if outcome != "rejected")
    superuser_db.expire_all()
    assert superuser_db.get(AgentWorkAssignment, assignment_id).status.value == winner


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_terminal_transition_replay_is_idempotent(
    superuser_db, make_verified_user, terminal
):
    owner, _password = make_verified_user()
    goal_id, task_id, agent_id = _foundation(superuser_db, owner.id)
    assignment = _create(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal_id,
        task_id=task_id,
        agent_id=agent_id,
    )
    transition_status(superuser_db, assignment=assignment, new_status="running")
    transition_status(superuser_db, assignment=assignment, new_status=terminal)
    transition_status(superuser_db, assignment=assignment, new_status=terminal)
    superuser_db.commit()
    events = superuser_db.execute(
        select(AgentWorkAssignmentEvent).where(
            AgentWorkAssignmentEvent.assignment_id == assignment.id,
            AgentWorkAssignmentEvent.event_type
            == AgentWorkAssignmentEventType.status_changed,
        )
    ).scalars().all()
    assert [event.detail.get("to") for event in events].count(terminal) == 1
