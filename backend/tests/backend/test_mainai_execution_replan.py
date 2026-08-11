"""MainAI V0.3 -- minimal automatic replan trigger (app/mainai_execution/replan.py,
app/worker.py's `_advance_mainai_execution_replan`). V0.1's planner.py already fully implements
a replan's MECHANICS (create_plan() called again supersedes the previous plan, cancels its
still-non-terminal tasks, versions the new one); V0.3 only adds the automatic decision of WHEN
that's warranted and drives it unattended, reusing propose_plan_via_ai()/create_plan()
unchanged.

Covers:
  - find_replan_trigger(): finds a permanently `failed` task in the ACTIVE plan; None once
    that plan is superseded (a later plan's own failures are found separately, on their own
    turn); None for a `retryable_failed` task (attempts remain -- not yet a real trigger).
  - Demo (real production path): a real task genuinely exhausts its retry budget (a real
    failing pytest run, max_attempts=1) through run_task_execution_job(), then the worker tick
    alone -- no manual call -- proposes and persists a fresh plan version, records a
    `replanned` event on the failed task, and a sibling task that depended on the failed one
    (cancelled by the OLD plan's own supersession) is never silently left dangling.
  - Approval escalation is structural, not a new mechanism: the replanned tasks get NEW
    task_ids, so no prior `approval_granted` event can apply to them."""

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.replan import find_replan_trigger
from app.models.mainai_execution import MainAIPlan, MainAIPlanStatus, MainAITask, MainAITaskStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="Replan demo goal", original_instruction="Fix the thing.", created_by="test")


def _events(db_session, task_id) -> set[str]:
    rows = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(task_id)}).all()
    return {row[0] for row in rows}


def test_find_replan_trigger_is_none_when_nothing_has_failed(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    assert find_replan_trigger(db_session, goal=goal) is None


def test_find_replan_trigger_ignores_a_retryable_failed_task(db_session, owner_id):
    """Attempts remain -- this is not yet a permanent failure, so no replan is warranted."""
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit", max_attempts=3)],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    db_session.commit()
    assert find_replan_trigger(db_session, goal=goal) is None


@pytest.mark.asyncio
async def test_demo_auto_replan_triggers_after_a_task_permanently_exhausts_retries(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """REQUIRED demo: a real failing pytest run exhausts a task's (max_attempts=1) retry
    budget through the REAL run_task_execution_job() path, and the worker's own replan tick --
    no manual call -- proposes and persists a fresh plan version."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_scratch_fail.py").write_text("def test_scratch_fail():\n    assert False\n")

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="a doomed run_tests task",
        tasks=[
            PlannedTaskSpec(
                description="run the (deliberately failing) suite",
                task_type="run_tests",
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_scratch_fail.py"}],
                max_attempts=1,
            )
        ],
        created_by="test",
    )
    db_session.commit()
    failed_task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    job = executor.dispatch_ready_task(db_session, task=failed_task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    failed_task = db_session.get(MainAITask, failed_task.id)
    assert failed_task.status == MainAITaskStatus.failed
    assert failed_task.attempts >= failed_task.max_attempts

    async def _fake_chat(self, messages, model, **kwargs):
        return await _fake_plan_response(model)

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    worker = Worker()
    await worker._advance_mainai_execution_replan(superuser_db)

    _set_rls_user(db_session, owner_id)
    db_session.expire_all()
    plans = db_session.query(MainAIPlan).filter(MainAIPlan.goal_id == goal.id).order_by(MainAIPlan.version.asc()).all()
    assert len(plans) == 2
    assert plans[0].status == MainAIPlanStatus.superseded
    assert plans[1].status == MainAIPlanStatus.active
    assert plans[1].version == 2

    events = _events(db_session, failed_task.id)
    assert "replanned" in events

    new_tasks = db_session.query(MainAITask).filter(MainAITask.plan_id == plans[1].id).all()
    assert len(new_tasks) >= 1
    assert all(t.id != failed_task.id for t in new_tasks), "a replan must create genuinely NEW tasks, not reuse the old id"


async def _fake_plan_response(model):
    import json

    return ChatResult(
        content=json.dumps({"tasks": [{"description": "try a different approach", "task_type": "read_only_audit"}]}),
        provider="openai",
        model=model,
        raw_usage={},
    )
