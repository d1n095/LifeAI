"""MainAI V0.3 -- cooperative cancellation of `running`/`waiting_ci` MainAITasks
(app/mainai_execution/executor.py's extended cancel_task(), app/mainai_execution/
execution_job.py's TaskCancelledCooperatively/_check_cancel_requested()).

Covers:
  A. cancel_task() on `running`: sets job.cancel_requested (reusing app/jobs/service.py's
     request_cancel() -- the SAME primitive corpus_review.py already uses), records a
     `cancel_requested` task event, task stays `running` (an API call alone can never kill work
     already in flight).
  B. cancel_task() on `waiting_ci`: immediate (no job in flight) -- cancels the durable wait and
     finalizes the task `cancelled`.
  C. Demos (real production path, never hand-constructed state): a cooperative cancel requested
     before dispatch stops the task at the very first safe checkpoint (before ANY AI call), and
     a cancel that arrives mid-flight (during the AI call) still lets an already-made LOCAL git
     commit stand -- it only stops the NEXT real side effect (push) -- proving cancellation
     never destroys already-durable work.

Real local Postgres (RLS included). Only the LLM provider and (for the real-worktree demo) the
GitHub client are faked, matching this test suite's own convention throughout."""

import subprocess

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.ci_wait import start_ci_wait
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.executor import TaskNotCancellableError
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_wait import MainAITaskWait, MainAITaskWaitStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
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
    return planner.create_goal(db_session, owner_id=owner_id, title="Test goal", original_instruction="Ship it.", created_by="test")


def _single_task_plan(db_session, goal, **spec_kwargs) -> MainAITask:
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "read_only_audit")
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def _fake_chat(response_text: str, *, on_call=None):
    async def _chat(self, messages, model, **kwargs):
        if on_call is not None:
            on_call()
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _events(db_session, task_id) -> set[str]:
    rows = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(task_id)}).all()
    return {row[0] for row in rows}


# ---------------------------------------------------------------- A. cancel_task() on `running`


def test_cancel_task_on_running_sets_job_cancel_requested_and_stays_running(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="read_only_audit")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    executor.cancel_task(db_session, task=task, cancelled_by="founder@example.com", cancelled_by_id=owner_id, reason="stop it")
    db_session.commit()

    task = db_session.get(MainAITask, task.id)
    job = db_session.get(MainAIJob, job.id)
    assert task.status == MainAITaskStatus.running
    assert job.cancel_requested is True
    assert "cancel_requested" in _events(db_session, task.id)


def test_cancel_task_on_running_without_cancelled_by_id_is_rejected(db_session, owner_id):
    """A cooperative cancel MUST be attributable (request_cancel()'s own audit trail requires
    a real requesting user) -- a caller that can't identify who asked gets a clean error, never
    a silent no-op or an unattributed cancel."""
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="read_only_audit")
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    with pytest.raises(TaskNotCancellableError):
        executor.cancel_task(db_session, task=task, cancelled_by="founder@example.com")
    db_session.rollback()


# ---------------------------------------------------------------- B. cancel_task() on `waiting_ci`


def test_cancel_task_on_waiting_ci_cancels_the_wait_and_finalizes_the_task(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    task.status = MainAITaskStatus.waiting_ci
    db_session.commit()

    executor.cancel_task(db_session, task=task, cancelled_by="founder@example.com", cancelled_by_id=owner_id, reason="no longer needed")
    db_session.commit()

    task = db_session.get(MainAITask, task.id)
    wait = db_session.query(MainAITaskWait).filter(MainAITaskWait.task_id == task.id).one()
    assert task.status == MainAITaskStatus.cancelled
    assert task.completed_at is not None
    assert wait.status == MainAITaskWaitStatus.cancelled
    assert "cancelled" in _events(db_session, task.id)


# ---------------------------------------------------------------- C. real end-to-end demos


@pytest.mark.asyncio
async def test_demo_cancel_requested_before_dispatch_stops_before_any_ai_call(db_session, superuser_db, owner_id, monkeypatch):
    """REQUIRED demo: a cancel requested while the task is still `running` (before the worker
    even picks up the job) is honored at the very first safe checkpoint, through the REAL
    run_task_execution_job() path -- the AI call never happens at all."""
    ai_called = {"n": 0}
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("should never be seen", on_call=lambda: ai_called.__setitem__("n", ai_called["n"] + 1)))

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="read_only_audit")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    executor.cancel_task(db_session, task=task, cancelled_by="founder@example.com", cancelled_by_id=owner_id, reason="changed my mind")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    db_session.refresh(job)

    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    task = db_session.get(MainAITask, task.id)
    job = db_session.get(MainAIJob, job.id)
    assert ai_called["n"] == 0, "the AI call must never have happened"
    assert task.status == MainAITaskStatus.cancelled
    assert task.completed_at is not None
    assert job.status == MainAIJobStatus.cancelled
    events = _events(db_session, task.id)
    assert "cancelling" in events
    assert "cancelled" in events
    assert "completed" not in events
    assert "failed" not in events


@pytest.mark.asyncio
async def test_demo_cancel_arriving_mid_flight_preserves_the_already_made_local_commit_and_never_pushes(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """REQUIRED demo: a cancel that arrives WHILE the AI call is in flight (after dispatch, but
    before the local git commit that call's response produces) is caught at the NEXT safe
    checkpoint -- after the local commit is already durable, before the one irreversible real
    network side effect (a real `git push`) in this whole function. The local commit and its
    worktree row must never be destroyed."""
    from app.integrations.github_client import GitHubClient
    from app.mainai_execution import worktree as worktree_module
    from app.models.mainai_recovery import MainAITaskWorktree

    remote_path = tmp_path / "bare-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote_path)], check=True)
    seed_path = tmp_path / "seed-clone"
    subprocess.run(["git", "clone", "-q", str(remote_path), str(seed_path)], check=True)
    (seed_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed_path), "config", "user.email", "seed@test.local"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "config", "user.name", "Seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "checkout", "-q", "-b", worktree_module.BASE_BRANCH], check=True)
    subprocess.run(["git", "-C", str(seed_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed_path), "push", "-q", "origin", worktree_module.BASE_BRANCH], check=True)

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(remote_path))

    async def _fake_get_ref(self, branch: str) -> str:
        result = subprocess.run(["git", "-C", str(remote_path), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True)
        return result.stdout.strip()

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="repo_edit", verification_plan=[])
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    db_session.refresh(job)

    def _request_cancel_mid_flight():
        # Simulates the founder's cancel click landing WHILE the AI call is in flight --
        # exactly the same durable job.cancel_requested column execution_job.py's own
        # checkpoints read, set directly here (not via executor.cancel_task()) to isolate what
        # this test is actually proving: that execution_job.py's OWN checkpoint reacts
        # correctly, independent of which caller set the flag.
        superuser_db.execute(sa_text("UPDATE mainai_jobs SET cancel_requested = true WHERE id = :id"), {"id": str(job.id)})
        superuser_db.commit()

    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat('{"files": [{"path": "x.py", "content": "x = 1\\n"}], "commit_message": "test edit"}', on_call=_request_cancel_mid_flight),
    )

    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    task = db_session.get(MainAITask, task.id)
    job = db_session.get(MainAIJob, job.id)
    assert task.status == MainAITaskStatus.cancelled
    assert job.status == MainAIJobStatus.cancelled

    worktree = db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == job.id).one()
    assert worktree.current_commit is not None, "the real local commit must still be durably recorded"
    # And it was never pushed -- the branch must not exist on the bare remote at all.
    branches = subprocess.run(["git", "-C", str(remote_path), "branch", "--list", worktree.branch], capture_output=True, text=True).stdout
    assert branches.strip() == "", "a cooperative cancel must never push"
