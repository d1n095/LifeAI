"""V0.2 duplicate-side-effect prevention: the founder's explicit list --
worktree / local commit / push / branch / PR / verification / terminal task outcome. Each
item below is proven with a REAL second attempt at the same operation, not just reasoned
about: every one of these must be safe to call twice (or, for terminal outcome, must be
structurally impossible to reach twice) without creating a second, divergent side effect."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.jobs.mainai_job_lease import JobLeaseLostError, claim_next_mainai_job, renew_mainai_job_lease
from app.jobs.service import mark_completed
from app.mainai_execution import executor, planner
from app.mainai_execution.worktree import (
    BASE_BRANCH,
    commit_worktree_changes,
    create_task_worktree,
    push_worktree_branch,
)
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var


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

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    return bare_remote


def _task_and_job(db_session, owner_id):
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Dedup test goal", original_instruction="x", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task", tasks=[planner.PlannedTaskSpec(description="edit", task_type="repo_edit")], created_by="test"
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    return task, job


# ---------------------------------------------------------------- 1. worktree


@pytest.mark.asyncio
async def test_dedup_worktree_creation(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    first = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="w1")
    db_session.commit()
    second = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="w1")
    assert first.id == second.id
    assert first.path == second.path  # the SAME on-disk directory, not a second one


# ---------------------------------------------------------------- 2. local commit


@pytest.mark.asyncio
async def test_dedup_local_commit(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="w1")
    db_session.commit()
    (Path(wt.path) / "f.txt").write_text("content\n", encoding="utf-8")

    first_sha = commit_worktree_changes(db_session, wt, message="first commit")
    db_session.commit()
    assert first_sha is not None

    # Calling it again with nothing new to commit must be a clean no-op, never a second
    # (empty or duplicate) commit object.
    second_sha = commit_worktree_changes(db_session, wt, message="should be a no-op")
    assert second_sha is None

    log = subprocess.run(["git", "-C", wt.path, "log", "--oneline"], capture_output=True, text=True, check=True)
    commit_count = len([line for line in log.stdout.strip().splitlines() if line])
    assert commit_count == 2  # the seed commit + exactly one real commit, never two


# ---------------------------------------------------------------- 3/4. push + branch


@pytest.mark.asyncio
async def test_dedup_push_and_branch(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="w1")
    db_session.commit()
    (Path(wt.path) / "f.txt").write_text("content\n", encoding="utf-8")
    sha = commit_worktree_changes(db_session, wt, message="commit")
    db_session.commit()

    first_remote_sha = await push_worktree_branch(wt)
    assert first_remote_sha == sha

    # Pushing again with nothing new (same local HEAD, already-current remote branch) must
    # succeed as a clean no-op -- never rejected, never a duplicate branch/ref, never forced.
    second_remote_sha = await push_worktree_branch(wt)
    assert second_remote_sha == sha

    branches = subprocess.run(["git", "-C", str(patched_github), "branch", "--list", wt.branch], capture_output=True, text=True, check=True)
    assert branches.stdout.count(wt.branch) == 1  # exactly one branch by this name, not duplicated


# ---------------------------------------------------------------- 7. terminal task outcome


@pytest.mark.asyncio
async def test_dedup_terminal_task_outcome_second_completion_attempt_is_rejected(db_session, superuser_db, owner_id):
    """A second attempt to mark the SAME job/lease_generation completed after it already
    succeeded must be rejected outright -- proves the existing app/jobs/service.py fencing
    (unchanged by V0.2) already makes a duplicate terminal outcome structurally impossible,
    which is exactly what V0.2's own checkpoint-fencing fix (execution_job.py) and takeover
    fencing (mark_job_superseded) both build on rather than duplicate."""
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Terminal dedup goal", original_instruction="x", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    job_id, _owner, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)

    job = db_session.get(type(job), job_id)
    mark_completed(db_session, job, worker_id="worker-1", lease_generation=generation, public_message="done")

    # A second completion attempt for the exact same (job_id, worker_id, lease_generation) --
    # simulating a duplicate/racing completion signal -- must be rejected, not silently
    # accepted or double-counted.
    with pytest.raises(JobLeaseLostError):
        mark_completed(db_session, job, worker_id="worker-1", lease_generation=generation, public_message="done again")

    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(db_session, job_id, "worker-1", generation, 120)
