"""V0.2 minimal API surface (the three endpoints added to app/routers/mainai_execution.py):
  GET  /tasks/{task_id}/recovery      -- list recovery records for a task
  POST /tasks/{task_id}/recover       -- run the REAL pipeline (detect/inspect/classify/takeover)
  POST /recovery/{record_id}/approve  -- grant the V0.2 approval gate

Real local Postgres (RLS included) through the real FastAPI TestClient, real local git (a bare
repo standing in for GitHub, same pattern every other V0.2 recovery test file uses) -- never a
manual shortcut that calls recovery_*.py functions directly to fake what the endpoint itself is
supposed to do. Only the LLM provider is faked."""

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.worktree import BASE_BRANCH, commit_worktree_changes, create_task_worktree, push_worktree_branch
from app.models.mainai_execution import MainAITask
from app.models.mainai_job import MainAIJob
from app.request_context import current_user_id as current_user_id_var

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
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


def _founder_id():
    from app.founder import FOUNDER_USER_ID

    return FOUNDER_USER_ID


@pytest.fixture
def bare_remote(tmp_path) -> Path:
    remote_path = tmp_path / "bare-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote_path)], check=True)
    seed_path = tmp_path / "seed-clone"
    subprocess.run(["git", "clone", "-q", str(remote_path), str(seed_path)], check=True)
    (seed_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_path), "config", "user.email", "seed@test.local"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "checkout", "-q", "-b", BASE_BRANCH], check=True)
    subprocess.run(["git", "-C", str(seed_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "push", "-q", "origin", BASE_BRANCH], check=True)
    return remote_path


@pytest.fixture
def patched_github(monkeypatch, bare_remote):
    from app.config import get_settings
    from app.mainai_execution import worktree as worktree_module

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(bare_remote))

    async def _fake_get_ref(self, branch: str) -> str:
        result = subprocess.run(["git", "-C", str(bare_remote), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True)
        if result.returncode != 0:
            raise GitHubClientError(f"unknown ref {branch}")
        return result.stdout.strip()

    async def _fake_list_prs(self, *, head: str, base: str, state: str = "all") -> list[dict]:
        return []

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_prs)
    return {"remote": bare_remote}


def _dispatch_and_kill(db_session, superuser_db, owner_id, *, task_type: str) -> tuple[MainAITask, MainAIJob]:
    """Real dispatch + real claim + real lease expiry -- the exact state a dead worker leaves
    behind. Any git work the "dead worker" is meant to have done happens by the CALLER, between
    dispatch/claim and the lease kill below, mirroring the demos file's own ordering discipline
    (a worker dies AFTER acting, never before its claim)."""
    _set_rls_user(db_session, owner_id)
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Recovery API test goal", original_instruction="x", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(description="do it", task_type=task_type, verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)

    claim_next_mainai_job(superuser_db, "dead-worker", 120)
    _set_rls_user(db_session, owner_id)
    db_session.refresh(task)
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    return task, dead_job


def _kill_lease(superuser_db, db_session, owner_id, job_id) -> None:
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(job_id)})
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)


# ---------------------------------------------------------------- A. auth


def test_recovery_api_requires_authentication_for_every_endpoint(client):
    task_id = str(uuid.uuid4())
    record_id = str(uuid.uuid4())
    assert client.get(f"/api/mainai/execution/tasks/{task_id}/recovery").status_code in (401, 403)
    assert client.post(f"/api/mainai/execution/tasks/{task_id}/recover").status_code in (401, 403)
    assert client.post(f"/api/mainai/execution/recovery/{record_id}/approve").status_code in (401, 403)


# ---------------------------------------------------------------- B. 404s + 409


def test_recovery_api_list_returns_404_for_an_unknown_task(client):
    _login(client)
    res = client.get(f"/api/mainai/execution/tasks/{uuid.uuid4()}/recovery")
    assert res.status_code == 404


def test_recovery_api_recover_returns_404_for_an_unknown_task(client):
    csrf = _login(client)
    res = client.post(f"/api/mainai/execution/tasks/{uuid.uuid4()}/recover", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 404


def test_recovery_api_approve_returns_404_for_an_unknown_record(client):
    csrf = _login(client)
    res = client.post(f"/api/mainai/execution/recovery/{uuid.uuid4()}/approve", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 404


def test_recovery_api_recover_returns_409_when_the_task_has_no_dispatched_job(client, db_session):
    csrf = _login(client)
    _set_rls_user(db_session, _founder_id())
    goal = planner.create_goal(db_session, owner_id=_founder_id(), title="No job goal", original_instruction="x", created_by="test")
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="x", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/recover", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 409


# ---------------------------------------------------------------- C. real recovery through the API


def test_recovery_api_recovers_a_nothing_done_task_end_to_end(client, db_session, superuser_db):
    """No approval needed: the endpoint itself runs detect -> inspect -> classify -> takeover
    and comes back with a completed recovery record + a genuinely new dispatched job."""
    csrf = _login(client)
    owner_id = _founder_id()
    task, dead_job = _dispatch_and_kill(db_session, superuser_db, owner_id, task_type="read_only_audit")
    dead_job_id = dead_job.id
    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)

    res = client.post(f"/api/mainai/execution/tasks/{task.id}/recover", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["classification"] == "NOTHING_DONE"
    assert body["status"] == "completed"
    assert body["task_id"] == str(task.id)
    assert body["takeover_job_id"] is not None
    assert body["takeover_job_id"] != str(dead_job_id)

    list_res = client.get(f"/api/mainai/execution/tasks/{task.id}/recovery")
    assert list_res.status_code == 200
    records = list_res.json()
    assert len(records) == 1
    assert records[0]["id"] == body["id"]

    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "superseded"


def test_recovery_api_pushed_no_pr_requires_approve_endpoint_before_takeover_continues(client, db_session, superuser_db, patched_github):
    """The founder-driven two-step flow: POST /recover comes back still `classified` (real
    external GitHub state, approval outstanding) -- takeover has NOT happened -- then POST
    /recovery/{id}/approve, then POST /recover again actually completes it."""
    csrf = _login(client)
    owner_id = _founder_id()
    task, dead_job = _dispatch_and_kill(db_session, superuser_db, owner_id, task_type="repo_edit")
    wt = None
    import asyncio

    async def _do_push():
        nonlocal wt
        wt = await create_task_worktree(db_session, task=task, job=dead_job, lease_generation=dead_job.lease_generation, executor_id="dead-worker")
        db_session.commit()
        (Path(wt.path) / "pushed.txt").write_text("real pushed content\n", encoding="utf-8")
        commit_worktree_changes(db_session, wt, message="pushed.txt")
        db_session.commit()
        await push_worktree_branch(wt)

    asyncio.get_event_loop().run_until_complete(_do_push())
    _kill_lease(superuser_db, db_session, owner_id, dead_job.id)

    first_res = client.post(f"/api/mainai/execution/tasks/{task.id}/recover", headers={"X-CSRF-Token": csrf})
    assert first_res.status_code == 201, first_res.text
    first_body = first_res.json()
    assert first_body["classification"] == "PUSHED_NO_PR"
    assert first_body["status"] == "classified"  # takeover did NOT run -- approval outstanding

    approve_res = client.post(f"/api/mainai/execution/recovery/{first_body['id']}/approve", headers={"X-CSRF-Token": csrf})
    assert approve_res.status_code == 200, approve_res.text

    second_res = client.post(f"/api/mainai/execution/tasks/{task.id}/recover", headers={"X-CSRF-Token": csrf})
    assert second_res.status_code == 201, second_res.text
    second_body = second_res.json()
    assert second_body["id"] == first_body["id"]  # same record, same job -- idempotent detect
    assert second_body["status"] == "completed"
    assert second_body["takeover_job_id"] is not None


# ---------------------------------------------------------------- D. owner isolation


def test_recovery_api_a_founder_cannot_see_another_owners_recovery_records(client, db_session, superuser_db, make_verified_user):
    _login(client)
    other, _ = make_verified_user()
    task, dead_job = _dispatch_and_kill(db_session, superuser_db, other.id, task_type="read_only_audit")
    _kill_lease(superuser_db, db_session, other.id, dead_job.id)

    res = client.get(f"/api/mainai/execution/tasks/{task.id}/recovery")
    assert res.status_code == 404
