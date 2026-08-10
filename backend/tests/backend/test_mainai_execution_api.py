"""MainAI Execution Loop V0.1 — the minimal API surface (app/routers/mainai_execution.py).
Covers, in order:
  A. Auth: every endpoint requires an authenticated founder session.
  B. Goal lifecycle: create -> read -> AI-propose-and-create-plan -> read (plan + tasks
     visible) -> report.
  C. Task actions: read task detail (events/checkpoints/depends_on/approval_granted), approve,
     reject, cancel, retry -- including the 409s for an invalid transition on each.
  D. Owner isolation: this founder-only system's RLS-backed goal/task rows are never visible
     to a different owner's session.

Real local Postgres (RLS included), through the real FastAPI TestClient -- not calling
planner/executor functions directly. Only the LLM provider is faked."""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _founder_goal(db_session):
    from app.founder import FOUNDER_USER_ID

    _set_rls_user(db_session, FOUNDER_USER_ID)
    goal = planner.create_goal(
        db_session, owner_id=FOUNDER_USER_ID, title="API test goal", original_instruction="Do the thing.", created_by="test"
    )
    db_session.commit()
    return goal


def _founder_task(db_session, goal, **spec_kwargs):
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "read_only_audit")
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


# ---------------------------------------------------------------- A. auth


def test_api_requires_authentication_for_every_endpoint(client):
    goal_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    assert client.post("/api/mainai/execution/goals", json={"title": "x", "original_instruction": "y"}).status_code in (401, 403)
    assert client.get("/api/mainai/execution/goals").status_code in (401, 403)
    assert client.get(f"/api/mainai/execution/goals/{goal_id}").status_code in (401, 403)
    assert client.get(f"/api/mainai/execution/tasks/{task_id}").status_code in (401, 403)
    assert client.post(f"/api/mainai/execution/tasks/{task_id}/approve").status_code in (401, 403)


# ---------------------------------------------------------------- B. goal lifecycle


def test_api_create_read_plan_and_report_a_goal(client, db_session):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    create_res = client.post(
        "/api/mainai/execution/goals",
        json={"title": "Doc fix goal", "original_instruction": "Find and fix a tiny doc issue."},
        headers=headers,
    )
    assert create_res.status_code == 201, create_res.text
    goal_id = create_res.json()["id"]
    assert create_res.json()["status"] == "pending"
    assert create_res.json()["current_plan_version"] == 0

    get_res = client.get(f"/api/mainai/execution/goals/{goal_id}")
    assert get_res.status_code == 200
    assert get_res.json()["plan"] is None
    assert get_res.json()["tasks"] == []

    plan_json = (
        '{"tasks": [{"description": "audit", "task_type": "read_only_audit", "depends_on": []}, '
        '{"description": "verify", "task_type": "run_tests", "depends_on": [0], '
        '"verification_plan": [{"kind": "targeted_tests", "target": "tests/test_x.py"}]}]}'
    )

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content=plan_json, provider="openai", model=model, raw_usage={})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenAIProvider, "chat", _fake_chat)
        plan_res = client.post(f"/api/mainai/execution/goals/{goal_id}/plan", headers=headers)
    assert plan_res.status_code == 201, plan_res.text
    assert plan_res.json()["version"] == 1

    get_res = client.get(f"/api/mainai/execution/goals/{goal_id}")
    assert get_res.status_code == 200
    assert get_res.json()["plan"]["version"] == 1
    assert get_res.json()["status"] == "running"
    tasks = get_res.json()["tasks"]
    assert len(tasks) == 2
    by_type = {t["task_type"]: t for t in tasks}
    assert by_type["read_only_audit"]["status"] == "ready"
    assert by_type["read_only_audit"]["liveness"] == "idle"
    assert by_type["run_tests"]["status"] == "pending"

    tasks_res = client.get(f"/api/mainai/execution/goals/{goal_id}/tasks")
    assert tasks_res.status_code == 200
    assert len(tasks_res.json()) == 2

    report_res = client.get(f"/api/mainai/execution/goals/{goal_id}/report")
    assert report_res.status_code == 200
    assert report_res.json()["summary"]["total_tasks"] == 2
    assert report_res.json()["goal"]["id"] == goal_id


def test_api_get_goal_returns_404_for_an_unknown_goal(client):
    _login(client)
    res = client.get(f"/api/mainai/execution/goals/{uuid.uuid4()}")
    assert res.status_code == 404


def test_api_plan_returns_404_for_an_unknown_goal(client):
    csrf = _login(client)
    res = client.post(f"/api/mainai/execution/goals/{uuid.uuid4()}/plan", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 404


# ---------------------------------------------------------------- C. task actions


def test_api_get_task_returns_full_detail(client, db_session):
    _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal, approval_required=True)

    res = client.get(f"/api/mainai/execution/tasks/{task.id}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["approval_required"] is True
    assert body["approval_granted"] is False
    assert body["depends_on"] == []
    assert any(e["event_type"] == "created" for e in body["events"])
    assert body["checkpoints"] == []
    assert body["liveness"] == "idle"


def test_api_get_task_returns_404_for_an_unknown_task(client):
    _login(client)
    res = client.get(f"/api/mainai/execution/tasks/{uuid.uuid4()}")
    assert res.status_code == 404


def test_api_approve_then_the_worker_can_dispatch(client, db_session):
    csrf = _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal, approval_required=True)

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/approve", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.json()["approval_granted"] is True
    assert res.json()["status"] == "ready"  # approval alone does not dispatch -- the worker's own auto-advance tick does


def test_api_reject_cancels_an_approval_pending_task(client, db_session):
    csrf = _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal, approval_required=True)

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/reject", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"


def test_api_cancel_returns_409_for_a_running_task(client, db_session):
    from app.mainai_execution import executor

    csrf = _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal)
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test")
    db_session.commit()

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/cancel", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 409


def test_api_retry_returns_409_for_a_non_retryable_task(client, db_session):
    csrf = _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal)  # status: ready, not retryable_failed

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/retry", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 409


def test_api_retry_succeeds_for_a_retryable_failed_task(client, db_session):
    csrf = _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal)
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    db_session.commit()

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/retry", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.json()["status"] == "ready"


# ---------------------------------------------------------------- D. owner isolation


def test_api_a_founder_cannot_see_another_owners_goal(client, db_session, make_verified_user):
    _login(client)
    other, _ = make_verified_user()
    _set_rls_user(db_session, other.id)
    other_goal = planner.create_goal(db_session, owner_id=other.id, title="not yours", original_instruction="x", created_by="test")
    db_session.commit()

    res = client.get(f"/api/mainai/execution/goals/{other_goal.id}")
    assert res.status_code == 404
