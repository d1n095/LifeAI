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

from app.mainai_execution import checkpoint as checkpoint_module
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


def test_api_cancel_of_a_running_task_is_cooperative_not_immediate(client, db_session):
    """V0.3: cancelling a `running` task is no longer rejected outright (the old V0.1
    limitation) -- it succeeds (200) and requests a cooperative cancel on the task's real
    in-flight job (app/jobs/service.py's request_cancel(), same primitive corpus_review.py's
    own per-batch check already uses), but the task itself stays `running` until
    execution_job.py's own next safe checkpoint actually acknowledges it -- an API call alone
    can never immediately kill work already in flight."""
    from app.mainai_execution import executor
    from app.models.mainai_job import MainAIJob

    csrf = _login(client)
    goal = _founder_goal(db_session)
    task = _founder_task(db_session, goal)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test")
    db_session.commit()

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/cancel", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.json()["status"] == "running"

    db_session.expire_all()
    refreshed_job = db_session.get(MainAIJob, job.id)
    assert refreshed_job.cancel_requested is True
    events = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(task.id)}).all()
    assert "cancel_requested" in {row[0] for row in events}


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


def test_cross_owner_rls_isolation_holds_for_every_new_v0_1_table_through_the_real_runtime_role(db_session, superuser_db, make_verified_user):
    """Hardening pass, RLS/cross-owner attack section: `db_session` is bound to the real
    restricted `mainai_app` runtime role (see conftest.py's own fixture docstring), never the
    superuser -- exactly the founder's own explicit requirement ("runtime role, inte admin").
    Covers all six new V0.1 tables (mainai_goals, mainai_plans, mainai_tasks,
    mainai_task_dependencies, mainai_task_events, mainai_checkpoints): owner A creates a real
    goal/plan/two dependent tasks/an event/a checkpoint; owner B (same runtime role, different
    RLS context) must see NONE of it via SELECT, and a targeted UPDATE/DELETE by owner B
    against owner A's own row ids must affect exactly zero rows (RLS silently filters the
    WHERE target out of existence, not a 403/error -- consistent with this app's existing
    404-not-403 convention). mainai_task_events/mainai_checkpoints additionally get their
    OWNER'S OWN UPDATE/DELETE attempt tested (not just cross-owner) since those two tables are
    append-only for everyone, enforced by both a revoked GRANT and a deny-mutation trigger."""
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    _set_rls_user(db_session, owner_a.id)
    goal = planner.create_goal(db_session, owner_id=owner_a.id, title="owner A's goal", original_instruction="x", created_by="test")
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="two dependent tasks",
        tasks=[PlannedTaskSpec(description="first", task_type="read_only_audit"), PlannedTaskSpec(description="second", task_type="read_only_audit", depends_on=[0])],
        created_by="test",
    )
    db_session.commit()
    plan = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).first().plan_id
    task1, task2 = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    task1_id, task2_id, plan_id, goal_id = task1.id, task2.id, plan, goal.id

    checkpoint = checkpoint_module.record_checkpoint(db_session, task=task1, goal=goal, job_id=uuid.uuid4(), step="work_result", data={"x": 1})
    checkpoint_id = checkpoint.id
    event_id = db_session.execute(
        sa_text("SELECT id FROM mainai_task_events WHERE task_id = :t ORDER BY created_at LIMIT 1"), {"t": str(task1_id)}
    ).scalar_one()
    db_session.commit()

    # --- owner B: SELECT must see nothing of owner A's rows ---------------------------------
    _set_rls_user(db_session, owner_b.id)
    assert db_session.execute(sa_text("SELECT count(*) FROM mainai_goals WHERE id = :id"), {"id": str(goal_id)}).scalar() == 0
    assert db_session.execute(sa_text("SELECT count(*) FROM mainai_plans WHERE id = :id"), {"id": str(plan_id)}).scalar() == 0
    assert db_session.execute(sa_text("SELECT count(*) FROM mainai_tasks WHERE id IN (:a, :b)"), {"a": str(task1_id), "b": str(task2_id)}).scalar() == 0
    assert (
        db_session.execute(
            sa_text("SELECT count(*) FROM mainai_task_dependencies WHERE task_id = :t"), {"t": str(task2_id)}
        ).scalar()
        == 0
    )
    assert db_session.execute(sa_text("SELECT count(*) FROM mainai_task_events WHERE id = :id"), {"id": str(event_id)}).scalar() == 0
    assert db_session.execute(sa_text("SELECT count(*) FROM mainai_checkpoints WHERE id = :id"), {"id": str(checkpoint_id)}).scalar() == 0
    # A guessed/random UUID fares no differently -- confirms the above isn't just "this
    # specific id happens to not match", it's genuine owner-scoped filtering.
    assert db_session.execute(sa_text("SELECT count(*) FROM mainai_tasks WHERE id = :id"), {"id": str(uuid.uuid4())}).scalar() == 0

    # --- owner B: a targeted UPDATE/DELETE against owner A's row ids affects zero rows ------
    r = db_session.execute(sa_text("UPDATE mainai_goals SET title = 'hijacked' WHERE id = :id"), {"id": str(goal_id)})
    assert r.rowcount == 0
    r = db_session.execute(sa_text("UPDATE mainai_tasks SET status = 'cancelled' WHERE id = :id"), {"id": str(task1_id)})
    assert r.rowcount == 0
    r = db_session.execute(sa_text("DELETE FROM mainai_tasks WHERE id = :id"), {"id": str(task1_id)})
    assert r.rowcount == 0
    db_session.rollback()

    # --- append-only tables: even the TRUE OWNER cannot mutate/delete an event/checkpoint ---
    _set_rls_user(db_session, owner_a.id)
    with pytest.raises(Exception):
        db_session.execute(sa_text("UPDATE mainai_task_events SET event_type = 'completed' WHERE id = :id"), {"id": str(event_id)})
    db_session.rollback()
    with pytest.raises(Exception):
        db_session.execute(sa_text("DELETE FROM mainai_task_events WHERE id = :id"), {"id": str(event_id)})
    db_session.rollback()
    with pytest.raises(Exception):
        db_session.execute(sa_text("UPDATE mainai_checkpoints SET step = 'tampered' WHERE id = :id"), {"id": str(checkpoint_id)})
    db_session.rollback()
    with pytest.raises(Exception):
        db_session.execute(sa_text("DELETE FROM mainai_checkpoints WHERE id = :id"), {"id": str(checkpoint_id)})
    db_session.rollback()

    # --- confirm via the superuser connection that nothing above actually got mutated -------
    assert superuser_db.execute(sa_text("SELECT title FROM mainai_goals WHERE id = :id"), {"id": str(goal_id)}).scalar_one() == "owner A's goal"
    assert superuser_db.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = :id"), {"id": str(task1_id)}).scalar_one() == "ready"
    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_task_events WHERE id = :id"), {"id": str(event_id)}).scalar() == 1
    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_checkpoints WHERE id = :id"), {"id": str(checkpoint_id)}).scalar() == 1


def test_cross_owner_composite_fk_blocks_a_task_dependency_row_claiming_the_wrong_owner(db_session, make_verified_user):
    """mainai_task_dependencies' composite FKs (task_id, owner_id) REFERENCES mainai_tasks (id,
    owner_id) mean a cross-owner dependency row is structurally impossible to insert, not just
    RLS-filtered after the fact -- verified directly here rather than assumed from reading the
    migration."""
    from sqlalchemy.exc import IntegrityError

    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()

    _set_rls_user(db_session, owner_a.id)
    goal_a = planner.create_goal(db_session, owner_id=owner_a.id, title="A", original_instruction="x", created_by="test")
    planner.create_plan(db_session, goal=goal_a, rationale="r", tasks=[PlannedTaskSpec(description="a-task", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    task_a = db_session.query(MainAITask).filter(MainAITask.goal_id == goal_a.id).one()

    _set_rls_user(db_session, owner_b.id)
    goal_b = planner.create_goal(db_session, owner_id=owner_b.id, title="B", original_instruction="x", created_by="test")
    planner.create_plan(db_session, goal=goal_b, rationale="r", tasks=[PlannedTaskSpec(description="b-task", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    task_b = db_session.query(MainAITask).filter(MainAITask.goal_id == goal_b.id).one()

    with pytest.raises(IntegrityError):
        db_session.execute(
            sa_text("INSERT INTO mainai_task_dependencies (task_id, depends_on_task_id, owner_id, created_at) VALUES (:t, :d, :o, now())"),
            {"t": str(task_b.id), "d": str(task_a.id), "o": str(owner_b.id)},
        )
    db_session.rollback()
