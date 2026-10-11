"""Path A `agent_assignment` hook: the task type is known to the planner, dispatch still goes
through the Founder approval gate, the executor routes the task to
`app.agent_coordination.session.run_agent_assignment_task`, and a handler failure becomes a
`TaskExecutionError` -- all through the real `dispatch_ready_task()` /
`run_task_execution_job()` path.

No agent session is ever launched here: an autouse guard makes any subprocess start from the
agent adapter module fail the test."""

from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination import adapters as agent_adapters
from app.agent_coordination import session as agent_session
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _no_agent_process_may_start(monkeypatch):
    async def _refuse(*args, **kwargs):
        raise AssertionError(f"an agent subprocess was started during a test: {args[:1]}")

    monkeypatch.setattr(agent_adapters.asyncio, "create_subprocess_exec", _refuse)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _agent_assignment_task(db_session, owner_id, *, approval_policy="standard_repo_work"):
    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="Ask an agent", original_instruction="Ask Claude Code to review the adapter.",
        created_by="test", approval_policy=approval_policy,
    )
    planner.create_plan(
        db_session, goal=goal, rationale="agent assignment hook",
        tasks=[PlannedTaskSpec(description="Review app/agent_coordination/adapters.py.", task_type="agent_assignment")], created_by="test",
    )
    db_session.commit()
    return goal, db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


async def _dispatch_and_run(db_session, superuser_db, owner_id, goal, task):
    grant_task_approval(db_session, task=task, approved_by="founder")
    db_session.commit()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)
    return job


def _event_details(db, task_id) -> list[dict]:
    rows = db.execute(sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id"), {"id": str(task_id)}).all()
    return [row[0] or {} for row in rows]


def test_agent_assignment_is_a_known_task_type():
    assert "agent_assignment" in planner.KNOWN_TASK_TYPES


def test_agent_assignment_dispatch_requires_founder_approval_even_under_the_standard_policy(db_session, superuser_db, owner_id):
    goal, task = _agent_assignment_task(db_session, owner_id)
    with pytest.raises(ApprovalRequiredError):
        executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.rollback()
    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}).scalar() == 0

    grant_task_approval(db_session, task=task, approved_by="founder")
    db_session.commit()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    assert job is not None


@pytest.mark.asyncio
async def test_agent_assignment_task_is_routed_to_the_session_handler(db_session, superuser_db, owner_id, monkeypatch):
    calls = []

    async def _spy(db, task):
        calls.append(task.id)
        return {"assignment_id": "a", "agent_key": "claude-code", "session_id": "sess-0001abcd", "agent_final_message": "ok",
                "agent_final_message_is_self_report": True, "assignment_status": "waiting_review"}

    monkeypatch.setattr(agent_session, "run_agent_assignment_task", _spy)
    goal, task = _agent_assignment_task(db_session, owner_id)
    job = await _dispatch_and_run(db_session, superuser_db, owner_id, goal, task)

    assert calls == [task.id]
    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.completed
    assert superuser_db.get(MainAIJob, job.id) is not None
    assert any(d.get("work_result", {}).get("session_id") == "sess-0001abcd" for d in _event_details(superuser_db, task.id))


@pytest.mark.asyncio
async def test_handler_failure_becomes_a_task_execution_error(db_session, superuser_db, owner_id, monkeypatch):
    async def _fail(db, task):
        raise agent_session.AgentSessionError("ADAPTER_DISABLED", "disabled -- set LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE=true")

    monkeypatch.setattr(agent_session, "run_agent_assignment_task", _fail)
    goal, task = _agent_assignment_task(db_session, owner_id)
    await _dispatch_and_run(db_session, superuser_db, owner_id, goal, task)

    task = superuser_db.get(MainAITask, task.id)
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)
    assert "ADAPTER_DISABLED" in str(_event_details(superuser_db, task.id))


@pytest.mark.asyncio
async def test_real_handler_without_a_ready_assignment_fails_closed_and_launches_nothing(db_session, superuser_db, owner_id):
    goal, task = _agent_assignment_task(db_session, owner_id)
    await _dispatch_and_run(db_session, superuser_db, owner_id, goal, task)

    task = superuser_db.get(MainAITask, task.id)
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)
    assert "NO_UNIQUE_READY_ASSIGNMENT" in str(_event_details(superuser_db, task.id))
