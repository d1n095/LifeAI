"""Worker must close goals when every task is already terminal — not only on GET /report."""

from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
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


def test_worker_finalize_tick_closes_goal_when_all_tasks_are_terminal(db_session, owner_id):
    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="Done graph", original_instruction="Finish.", created_by="test"
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="one task",
        tasks=[PlannedTaskSpec(description="A", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()

    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    task.status = MainAITaskStatus.completed
    task.completed_at = datetime.utcnow()
    db_session.commit()

    db_session.refresh(goal)
    assert goal.status == MainAIGoalStatus.running
    assert goal.final_outcome is None

    Worker()._finalize_mainai_execution_goals(db_session)
    db_session.refresh(goal)

    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert goal.completed_at is not None


def test_worker_finalize_tick_leaves_goal_alone_while_a_task_is_still_open(db_session, owner_id):
    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="Open graph", original_instruction="Work.", created_by="test"
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="one task",
        tasks=[PlannedTaskSpec(description="A", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()

    Worker()._finalize_mainai_execution_goals(db_session)
    db_session.refresh(goal)

    assert goal.status == MainAIGoalStatus.running
    assert goal.final_outcome is None
