"""capability_unavailable at worker execution must finalize the owning MainAITask."""

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import planner
from app.mainai_execution.executor import dispatch_ready_task
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_runtime_contract import _CAPABILITY_WRITE_PROFILE
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus
from app.request_context import current_user_id as current_user_id_var
from app.worker import process_claimed_mainai_job


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


@pytest.mark.asyncio
async def test_capability_unavailable_finalizes_running_task_execution_task(
    db_session, superuser_db, owner_id, monkeypatch
):
    """Dispatch moves the task to running before the worker capability gate. If that gate
    fails the job without finalize, the task stays running on a terminal job until delayed
    auto-recovery — fail closed immediately instead."""
    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="cap gate", original_instruction="Do it.", created_by="test"
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
    job = dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test")
    db_session.commit()
    assert task.status == MainAITaskStatus.running

    job_id, claimed_owner, generation = claim_next_mainai_job(superuser_db, "worker-cap", 120)
    assert job_id == job.id
    assert claimed_owner == owner_id

    monkeypatch.setitem(_CAPABILITY_WRITE_PROFILE["task_execution"], "production_prohibited", True)
    monkeypatch.setattr(get_settings(), "environment", "production")

    _set_rls_user(db_session, owner_id)
    await process_claimed_mainai_job(db_session, job_id, owner_id, "worker-cap", generation, 120)

    refreshed_job = superuser_db.get(MainAIJob, job_id)
    assert refreshed_job.status == MainAIJobStatus.failed
    assert refreshed_job.error_category == MainAIJobErrorCategory.capability_unavailable.value

    db_session.expire_all()
    _set_rls_user(db_session, owner_id)
    refreshed_task = db_session.get(MainAITask, task.id)
    assert refreshed_task is not None
    assert refreshed_task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)
    assert refreshed_task.status != MainAITaskStatus.running
    assert refreshed_task.completed_at is None or refreshed_task.status == MainAITaskStatus.failed
