"""MainAI V0.3 -- automatic dead-agent recovery polling (app/worker.py's
`_advance_mainai_execution_auto_recovery`). V0.2 built the full recovery pipeline (detect ->
inspect -> classify -> [approval] -> takeover) but its own docs explicitly named "no worker/
background process runs recovery automatically" as a real, deliberate gap -- reachable only via
`POST /tasks/{id}/recover`. This file proves the worker tick now closes that gap by driving the
SAME pipeline (get_or_create_recovery_record -> inspect_recovery_record ->
classify_recovery_record -> execute_takeover) unattended, through the real dispatch/claim/
kill-lease/run_task_execution_job mechanics every V0.2 recovery demo already uses -- never a
hand-constructed recovery record.

Covers:
  - A genuinely dead job (real expired lease) with no work at all is auto-detected, classified
    NOTHING_DONE, and auto-taken-over -- the new attempt runs for real and the task completes,
    with zero founder action.
  - A job whose lease has NOT expired (still genuinely alive) is left completely untouched --
    the tick's detection condition must never fire on live work.
  - A classification that requires founder approval (PUSHED_NO_PR) is classified but NEVER
    auto-taken-over -- execute_takeover()'s own approval gate (recovery_approval.py) stops the
    tick exactly like it stops a founder-triggered call, and the tick must not auto-approve it."""

from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import record_checkpoint
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.worktree import BASE_BRANCH, commit_worktree_changes, create_task_worktree, push_worktree_branch
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_recovery import MainAIRecoveryRecord, MainAIRecoveryStatus, RecoveryClassification
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


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="Auto-recovery demo goal", original_instruction="Do it.", created_by="test")


def _dispatch_and_claim(db_session, superuser_db, owner_id, *, task_type: str) -> tuple[MainAITask, MainAIGoal, MainAIJob]:
    goal = _goal(db_session, owner_id)
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
    return task, goal, dead_job


def _kill_lease(superuser_db, db_session, owner_id, job_id) -> None:
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(job_id)})
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)


@pytest.mark.asyncio
async def test_demo_auto_recovery_tick_detects_and_takes_over_a_dead_job_with_no_founder_action(
    db_session, superuser_db, owner_id, monkeypatch
):
    """REQUIRED demo: the worker tick alone -- no call to POST /tasks/{id}/recover, no manual
    pipeline call -- finds the dead job, classifies it NOTHING_DONE, takes it over, and (since
    a real worker would next claim and run the fresh job) the task genuinely completes once
    that new job is claimed and run."""
    call_counter = [0]

    async def _counting_chat(self, messages, model, **kwargs):
        call_counter[0] += 1
        return ChatResult(content="Analysen visar inga problem.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _counting_chat)

    task, _goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="read_only_audit")
    dead_job_id = dead_job.id
    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)

    worker = Worker()
    await worker._advance_mainai_execution_auto_recovery(superuser_db)

    record = superuser_db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == dead_job_id).one()
    assert record.classification == RecoveryClassification.nothing_done
    assert record.status == MainAIRecoveryStatus.completed

    superuser_db.expire_all()
    old_job_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert old_job_row[0] == "superseded"

    _set_rls_user(db_session, owner_id)
    db_session.refresh(task)
    events = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(task.id)}).all()
    assert "auto_recovery_triggered" in {row[0] for row in events}

    # A real worker's next poll cycle claims and runs the fresh job -- proving the takeover
    # produced a genuinely usable, dispatchable job, not just a database row.
    from app.mainai_execution.execution_job import run_task_execution_job

    new_job_id = superuser_db.execute(
        sa_text("SELECT superseded_by_job_id FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}
    ).scalar()
    _, _, generation = claim_next_mainai_job(superuser_db, "recovery-worker-2", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, new_job_id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_counter[0] == 1
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_auto_recovery_tick_never_touches_a_job_whose_lease_has_not_expired(db_session, superuser_db, owner_id):
    """A genuinely live job (lease still valid) must never be treated as dead -- the tick's own
    detection query is the only thing standing between real, in-flight work and an unwanted
    takeover attempt."""
    task, _goal, live_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="read_only_audit")
    live_job_id = live_job.id
    # Lease is still valid (claim_next_mainai_job() above already set a real future expiry) --
    # deliberately NOT killed here.

    worker = Worker()
    await worker._advance_mainai_execution_auto_recovery(superuser_db)

    record = superuser_db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == live_job_id).one_or_none()
    assert record is None, "a live job must never get a recovery record at all"
    superuser_db.expire_all()
    job_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(live_job_id)}).one()
    assert job_row[0] == "running"


@pytest.mark.asyncio
async def test_auto_recovery_tick_classifies_but_never_auto_approves_pushed_no_pr(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """A classification that requires founder approval must be classified (that half IS
    unattended-safe -- it's read-only evidence gathering) but the tick must never call
    execute_takeover() past the approval gate on its own -- PUSHED_NO_PR's real code is already
    live on GitHub, and only a founder may decide to continue building on it."""
    import subprocess

    from app.config import get_settings
    from app.integrations.github_client import GitHubClient
    from app.mainai_execution import worktree as worktree_module

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

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_repo", "test-owner/test-repo")
    monkeypatch.setattr(settings, "github_token", "fake-token-not-used-over-network")
    monkeypatch.setattr(worktree_module, "_authed_remote_url", lambda repo, token: str(remote_path))

    async def _fake_get_ref(self, branch: str) -> str:
        result = subprocess.run(["git", "-C", str(remote_path), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True)
        return result.stdout.strip()

    async def _fake_list_prs(self, *, head: str, base: str, state: str = "all") -> list[dict]:
        return []

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_prs)

    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="repo_edit")
    wt = await create_task_worktree(db_session, task=task, job=dead_job, lease_generation=dead_job.lease_generation, executor_id="dead-worker")
    db_session.commit()
    (Path(wt.path) / "pushed.txt").write_text("real pushed content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()
    await push_worktree_branch(wt)
    record_checkpoint(db_session, task=task, goal=goal, job_id=dead_job.id, step="work_result", data={"work_result": {"files": ["pushed.txt"]}})
    db_session.commit()
    _kill_lease(superuser_db, db_session, owner_id, dead_job.id)

    worker = Worker()
    await worker._advance_mainai_execution_auto_recovery(superuser_db)

    record = superuser_db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == dead_job.id).one()
    assert record.classification == RecoveryClassification.pushed_no_pr
    assert record.status == MainAIRecoveryStatus.classified, "must stop at classified -- never auto-taken-over"
    assert record.manual_review_required is False  # PUSHED_NO_PR isn't manual-review -- it's approval-gated, a different thing

    superuser_db.expire_all()
    job_row = superuser_db.execute(sa_text("SELECT status, superseded_by_job_id FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job.id)}).one()
    assert job_row[0] == MainAIJobStatus.running.value, "the dead job must not have been superseded without founder approval"
    assert job_row[1] is None
