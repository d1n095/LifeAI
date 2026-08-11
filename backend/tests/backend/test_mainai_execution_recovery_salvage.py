"""V0.2 recovery pipeline stage 4: salvage (recovery_salvage.salvage_recovery_record()).
Real Postgres + real local git, same fixtures as test_mainai_execution_recovery.py. Verifies
salvage copies durable evidence FORWARD to a genuinely new mainai_jobs row (a second
dispatch_ready_task() call, mirroring what a real takeover does) such that V0.1's OWN existing
resume contract (checkpoint.py) picks it up -- never a parallel mechanism."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClient
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import latest_checkpoint_for_step, record_checkpoint
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_salvage import SalvageError, salvage_recovery_record
from app.mainai_execution.worktree import (
    BASE_BRANCH,
    commit_worktree_changes,
    create_task_worktree,
    push_worktree_branch,
    verify_worktree_ownership,
)
from app.models.mainai_execution import MainAIGoal, MainAITask
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAIRecoveryStatus, MainAITaskWorktree
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
            from app.integrations.github_client import GitHubClientError

            raise GitHubClientError(f"unknown ref {branch}")
        return result.stdout.strip()

    async def _fake_list_prs(self, *, head: str, base: str, state: str = "all") -> list[dict]:
        return []

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_prs)
    return {"remote": bare_remote}


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="Salvage test goal", original_instruction="Edit a file.", created_by="test")


def _task_and_job(db_session, owner_id, *, task_type="repo_edit") -> tuple[MainAITask, MainAIJob]:
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(description="do it", task_type=task_type)], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    return task, job


def _goal_for(db_session, task) -> MainAIGoal:
    return db_session.query(MainAIGoal).filter(MainAIGoal.id == task.goal_id).one()


def _mark_job_dead_and_retry_task(db_session, task, job) -> MainAIJob:
    """Mirrors what a real takeover does: mark the dead job's task retryable, then dispatch a
    genuinely NEW mainai_jobs row for it -- salvage forwards evidence from the OLD job to
    THIS new one."""
    from app.models.mainai_execution import MainAITaskStatus

    task.status = MainAITaskStatus.retryable_failed
    db_session.add(task)
    db_session.commit()
    task = executor.retry_task(db_session, task=task)
    db_session.commit()
    goal = _goal_for(db_session, task)
    new_job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker-2")
    db_session.commit()
    return new_job


async def _through_classification(db_session, task, job):
    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=job, record=record)
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    return record


@pytest.mark.asyncio
async def test_salvage_nothing_done_is_a_clean_noop(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    record = await _through_classification(db_session, task, job)
    goal = _goal_for(db_session, task)

    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    record = await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)
    db_session.commit()

    assert record.status == MainAIRecoveryStatus.salvaged
    assert record.salvage_action == "none"
    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="work_result") is None


@pytest.mark.asyncio
async def test_salvage_checkpointed_work_copies_the_real_work_result_forward(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    goal = _goal_for(db_session, task)
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"summary": "real prior work"}})
    db_session.commit()

    record = await _through_classification(db_session, task, job)
    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    record = await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)
    db_session.commit()

    assert "copied_work_result_checkpoint" in record.salvage_action
    copied = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="work_result")
    assert copied is not None
    assert copied.executor_state["work_result"] == {"summary": "real prior work"}


@pytest.mark.asyncio
async def test_salvage_pushed_no_pr_synthesizes_a_truthful_finalized_checkpoint(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()
    await push_worktree_branch(wt)
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"files": ["pushed.txt"]}})
    db_session.commit()

    record = await _through_classification(db_session, task, job)
    from app.models.mainai_recovery import RecoveryClassification

    assert record.classification == RecoveryClassification.pushed_no_pr

    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    record = await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)
    db_session.commit()

    assert "synthesized_finalized_checkpoint" in record.salvage_action
    finalize_checkpoint = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="finalized")
    assert finalize_checkpoint is not None
    assert finalize_checkpoint.executor_state["finalize_info"]["branch"] == wt.branch
    assert finalize_checkpoint.executor_state["finalize_info"]["proposed"] is False


# ---------------------------------------------------------------- branch/commit salvage safety


@pytest.mark.asyncio
async def test_salvage_refuses_when_remote_branch_tip_moved_since_inspection(db_session, owner_id, patched_github, tmp_path):
    """Branch/commit salvage safety check: inspection recorded `remote_branch_sha` at one
    point in time; something outside this system (a human, another process) then advances
    the same branch before salvage ever runs. Salvage must re-verify live and refuse to
    synthesize a `finalized` checkpoint for a tip it can no longer prove is still there --
    never trust the stale inspection snapshot."""
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()
    await push_worktree_branch(wt)
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"files": ["pushed.txt"]}})
    db_session.commit()

    record = await _through_classification(db_session, task, job)
    from app.models.mainai_recovery import RecoveryClassification

    assert record.classification == RecoveryClassification.pushed_no_pr
    inspected_sha = record.evidence["remote_branch_sha"]

    # Something outside this system advances the branch AFTER inspection, BEFORE salvage.
    other_clone = tmp_path / "other-clone-moved-tip"
    remote = patched_github["remote"]
    subprocess.run(["git", "clone", "-q", str(remote), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", wt.branch], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "other@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "Other"], check=True)
    (other_clone / "extra.txt").write_text("someone else's commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "add", "extra.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "external advance"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", wt.branch], check=True)
    moved_sha = subprocess.run(
        ["git", "-C", str(other_clone), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert moved_sha != inspected_sha  # sanity: the remote genuinely moved

    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    with pytest.raises(SalvageError, match="tip has moved"):
        await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)

    # No finalized checkpoint was ever synthesized for unprovable state.
    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="finalized") is None


@pytest.mark.asyncio
async def test_salvage_refuses_when_remote_branch_deleted_since_inspection(db_session, owner_id, patched_github, tmp_path):
    """Same safety check, the other failure shape: the branch existed at inspection time but
    is gone by the time salvage runs (merged and deleted, manually removed, etc.)."""
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "pushed.txt").write_text("pushed content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()
    await push_worktree_branch(wt)
    record_checkpoint(db_session, task=task, goal=goal, job_id=job.id, step="work_result", data={"work_result": {"files": ["pushed.txt"]}})
    db_session.commit()

    record = await _through_classification(db_session, task, job)
    from app.models.mainai_recovery import RecoveryClassification

    assert record.classification == RecoveryClassification.pushed_no_pr

    remote = patched_github["remote"]
    subprocess.run(["git", "-C", str(remote), "branch", "-D", wt.branch], check=True)

    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    with pytest.raises(SalvageError, match="no longer exists on the remote"):
        await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)

    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="finalized") is None


@pytest.mark.asyncio
async def test_salvage_local_uncommitted_work_rebinds_the_worktree_to_the_new_job(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "draft.txt").write_text("uncommitted\n", encoding="utf-8")

    record = await _through_classification(db_session, task, job)
    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    record = await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)
    db_session.commit()

    assert "rebound_worktree" in record.salvage_action
    rebound = db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == new_job.id).one()
    assert rebound.id == wt.id
    assert verify_worktree_ownership(rebound) is True
    # The file written under the OLD job's attempt is still really there -- salvage preserved
    # the actual filesystem content, not just a database row.
    assert (Path(rebound.path) / "draft.txt").read_text(encoding="utf-8") == "uncommitted\n"
    # The old job_id no longer owns any worktree row.
    assert db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == job.id).one_or_none() is None


@pytest.mark.asyncio
async def test_salvage_refuses_a_record_that_is_not_yet_classified(db_session, owner_id, patched_github):
    task, job = _task_and_job(db_session, owner_id, task_type="read_only_audit")
    goal = _goal_for(db_session, task)
    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()

    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    with pytest.raises(SalvageError):
        await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)


@pytest.mark.asyncio
async def test_salvage_refuses_a_non_auto_salvageable_classification(db_session, owner_id, patched_github, tmp_path):
    task, job = _task_and_job(db_session, owner_id)
    goal = _goal_for(db_session, task)
    wt = await create_task_worktree(db_session, task=task, job=job, lease_generation=1, executor_id="worker-1")
    db_session.commit()
    (Path(wt.path) / "mine.txt").write_text("mine\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="mine.txt")
    db_session.commit()

    other_clone = tmp_path / "other-clone"
    remote = patched_github["remote"]
    subprocess.run(["git", "clone", "-q", str(remote), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", "-b", wt.branch, wt.base_sha], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "other@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "Other"], check=True)
    (other_clone / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "add", "theirs.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "theirs"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", wt.branch], check=True)

    record = await _through_classification(db_session, task, job)
    from app.models.mainai_recovery import RecoveryClassification

    assert record.classification == RecoveryClassification.conflicted_state

    new_job = _mark_job_dead_and_retry_task(db_session, task, job)
    with pytest.raises(SalvageError):
        await salvage_recovery_record(db_session, task=task, goal=goal, record=record, new_job=new_job)
