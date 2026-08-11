"""V0.2 required demos: the founder's own explicit list of seven scenarios a real dead-agent
recovery must handle, each proven through the ACTUAL recovery loop end to end (detect ->
inspect -> classify -> [approval] -> salvage -> takeover -> a real worker claiming and running
the new job to completion) -- never a manual shortcut that merely asserts a classification in
isolation. Real Postgres, real local git (a bare repo standing in for GitHub, same pattern
every other V0.2 recovery test file already uses), real claim/lease/checkpoint mechanics.

  1. Dead BEFORE any work -- NOTHING_DONE.
  2. Dead AFTER a local edit -- LOCAL_UNCOMMITTED_WORK.
  3. Dead AFTER a local commit -- LOCAL_COMMITTED_NOT_PUSHED.
  4. Dead AFTER a push -- PUSHED_NO_PR (requires founder approval, V0.2 approval gate).
  5. Dead AFTER a PR was opened -- PR_EXISTS (also requires approval).
  6. A stale worker returns AFTER takeover -- REQUIRED: every write it attempts (lease
     renewal, checkpoint) must be rejected exactly like any other fenced-out stale lease.
  7. Ambiguous / genuinely conflicting state -- CONFLICTED_STATE: fail closed,
     manual_review_required, takeover refuses outright, nothing is touched or destroyed."""

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClient, GitHubClientError
from app.jobs.mainai_job_lease import JobLeaseLostError, claim_next_mainai_job, renew_mainai_job_lease
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import record_checkpoint
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_approval import grant_recovery_approval
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import TakeoverError, execute_takeover
from app.mainai_execution.worktree import BASE_BRANCH, commit_worktree_changes, create_task_worktree, push_worktree_branch
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAIRecoveryStatus, RecoveryClassification
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
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

    _prs: dict[str, list[dict]] = {}

    async def _fake_list_prs(self, *, head: str, base: str, state: str = "all") -> list[dict]:
        return _prs.get(head, [])

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_prs)
    return {"remote": bare_remote, "prs": _prs}


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="Recovery demo goal", original_instruction="Edit a file.", created_by="test")


def _dispatch_and_claim(db_session, superuser_db, owner_id, *, task_type: str) -> tuple[MainAITask, MainAIGoal, MainAIJob]:
    """The real mechanism every demo below starts from: a genuine dispatch, then a genuine
    claim (so the job is really `running`, not merely `queued`) -- the exact state a worker
    process is in right before it does its real work and then dies. No shortcut ever sets
    MainAIJob.status or MainAITask.status directly. Killing the lease (_kill_lease below)
    happens SEPARATELY, after each demo does whatever real work the dead worker is meant to
    have completed first -- a worker dies AFTER acting, never before its own claim."""
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
    """The real crash: this attempt's lease genuinely expires -- the ONLY thing that makes a
    job structurally dead (never a status flip)."""
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(job_id)})
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)


async def _through_classification(db_session, task, dead_job):
    record = get_or_create_recovery_record(db_session, task=task, job=dead_job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=dead_job, record=record)
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    return record


def _claim_and_run(db_session, superuser_db, owner_id, job_id):
    _, _, generation = claim_next_mainai_job(superuser_db, "recovery-worker-2", 120)
    _set_rls_user(db_session, owner_id)
    return generation


# ================================================================== Demo 1: dead BEFORE work


@pytest.mark.asyncio
async def test_demo_1_dead_before_any_work_recovers_via_nothing_done(db_session, superuser_db, owner_id, monkeypatch):
    """The worker died the instant it claimed the job -- before the real AI call, before any
    git touch. NOTHING_DONE is the only honest classification; the new attempt genuinely
    starts fresh and performs the REAL work for the first time."""
    call_counter = [0]

    def _counting_chat(response_text):
        real = _fake_chat(response_text)

        async def _wrapped(self, messages, model, **kwargs):
            call_counter[0] += 1
            return await real(self, messages, model, **kwargs)

        return _wrapped

    monkeypatch.setattr(OpenAIProvider, "chat", _counting_chat("Analysen visar inga problem."))

    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="read_only_audit")
    dead_job_id = dead_job.id
    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)  # died before touching anything at all

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.nothing_done

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()
    assert record.status == MainAIRecoveryStatus.completed

    generation = _claim_and_run(db_session, superuser_db, owner_id, new_job.id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_counter[0] == 1  # the REAL work finally happened, exactly once
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed
    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "superseded"


# ============================================================ Demo 2: dead AFTER local edit


@pytest.mark.asyncio
async def test_demo_2_dead_after_local_edit_recovers_via_local_uncommitted_work(
    db_session, superuser_db, owner_id, monkeypatch, patched_github
):
    """REQUIRED demo, hardening-pass Round 2: unlike the ORIGINAL version of this demo (which
    called worktree.py's create_task_worktree()/wrote a file directly and never ran
    run_task_execution_job() at all), this crash is driven through the REAL, unmodified
    execution_job.py repo_edit path -- the exact same reasoning test_mainai_execution_demos.py's
    own Demo 3 already documents for why this is the most faithful simulation of a genuine
    process crash available in a unit test. Only commit_worktree_changes() itself is made to
    raise (once), so everything up to and including the real AI call and the real file write
    to the real worktree happens for real; the worktree ROW's own durability (Round 2 (2/N)'s
    fix) is what makes LOCAL_UNCOMMITTED_WORK reachable from this crash point at all."""
    from app.mainai_execution import execution_job

    call_count = {"n": 0}

    async def _counting_chat(self, messages, model, **kwargs):
        call_count["n"] += 1
        return ChatResult(
            content='{"files": [{"path": "draft.txt", "content": "uncommitted real edit\\n"}], "commit_message": "add draft.txt"}',
            provider="openai",
            model=model,
            raw_usage={},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _counting_chat)

    real_commit_worktree_changes = execution_job.commit_worktree_changes
    commit_calls = {"n": 0}

    def _commit_that_crashes_on_first_call(db, worktree, *, message):
        commit_calls["n"] += 1
        if commit_calls["n"] == 1:
            raise RuntimeError("simulated worker crash after local edit, before local commit")
        return real_commit_worktree_changes(db, worktree, message=message)

    monkeypatch.setattr(execution_job, "commit_worktree_changes", _commit_that_crashes_on_first_call)

    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="repo_edit")
    dead_job_id = dead_job.id

    with pytest.raises(RuntimeError, match="simulated worker crash after local edit"):
        await run_task_execution_job(
            db_session, dead_job_id, owner_id, worker_id="dead-worker", lease_generation=dead_job.lease_generation, lease_seconds=120
        )
    db_session.rollback()  # nothing further ever gets committed after a real crash (matches the established idiom elsewhere in this suite) -- releases the row lock a real dropped connection would also release

    # The real production evidence a genuine crash leaves behind: a durable worktree row
    # (committed by _handle_repo_edit() BEFORE the AI call, per the Round 2 durability fix),
    # with the AI's real file write sitting genuinely uncommitted on disk.
    from app.models.mainai_recovery import MainAITaskWorktree

    dead_worktree = db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == dead_job_id).one()
    assert (Path(dead_worktree.path) / "draft.txt").read_text(encoding="utf-8") == "uncommitted real edit\n"
    assert call_count["n"] == 1

    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.local_uncommitted_work

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    rebound = db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == new_job.id).one()
    assert rebound.id == dead_worktree.id
    assert (Path(rebound.path) / "draft.txt").read_text(encoding="utf-8") == "uncommitted real edit\n"
    assert db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == dead_job_id).one_or_none() is None

    # The resumed attempt runs through the real, unmodified poll-loop entry point too -- the
    # salvaged worktree's uncommitted content is committed for real and pushed, with NO second
    # AI call (dedup requirement: the real edit already existed, redoing it would be wasted,
    # possibly divergent, AI work).
    generation = _claim_and_run(db_session, superuser_db, owner_id, new_job.id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_count["n"] == 1  # never called a second time
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed
    remote_tip_content = subprocess.run(
        ["git", "-C", str(patched_github["remote"]), "show", f"{rebound.branch}:draft.txt"], capture_output=True, text=True, check=True
    ).stdout
    assert remote_tip_content == "uncommitted real edit\n"  # the salvaged edit, genuinely pushed


# ========================================================== Demo 3: dead AFTER local commit


@pytest.mark.asyncio
async def test_demo_3_dead_after_local_commit_recovers_via_local_committed_not_pushed(
    db_session, superuser_db, owner_id, monkeypatch, patched_github
):
    """REQUIRED demo, hardening-pass Round 2: same reasoning as Demo 2 above -- driven through
    the REAL execution_job.py repo_edit path, not a hand-constructed worktree.py call. Only
    push_worktree_branch() itself is made to raise (once), so the real AI call, real file
    write, AND real local `git commit` all happen for real before the simulated crash; the
    durably-committed `worktree.current_commit` DB column (Round 2 (2/N)'s fix -- the exact
    fact recovery_classifier.py's LOCAL_COMMITTED_NOT_PUSHED branch reads) is what makes this
    classification reachable from a genuine crash at this exact point."""
    from app.mainai_execution import execution_job

    call_count = {"n": 0}

    async def _counting_chat(self, messages, model, **kwargs):
        call_count["n"] += 1
        return ChatResult(
            content='{"files": [{"path": "committed.txt", "content": "committed real change\\n"}], "commit_message": "add committed.txt"}',
            provider="openai",
            model=model,
            raw_usage={},
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _counting_chat)

    real_push_worktree_branch = execution_job.push_worktree_branch
    push_calls = {"n": 0}

    async def _push_that_crashes_on_first_call(worktree):
        push_calls["n"] += 1
        if push_calls["n"] == 1:
            raise RuntimeError("simulated worker crash after local commit, before push")
        return await real_push_worktree_branch(worktree)

    monkeypatch.setattr(execution_job, "push_worktree_branch", _push_that_crashes_on_first_call)

    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="repo_edit")
    dead_job_id = dead_job.id

    with pytest.raises(RuntimeError, match="simulated worker crash after local commit"):
        await run_task_execution_job(
            db_session, dead_job_id, owner_id, worker_id="dead-worker", lease_generation=dead_job.lease_generation, lease_seconds=120
        )
    db_session.rollback()  # nothing further ever gets committed after a real crash -- releases the row lock a real dropped connection would also release

    from app.models.mainai_recovery import MainAITaskWorktree

    dead_worktree = db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == dead_job_id).one()
    local_sha = subprocess.run(
        ["git", "-C", dead_worktree.path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert dead_worktree.current_commit == local_sha  # durably recorded BEFORE the crash
    assert call_count["n"] == 1
    assert push_calls["n"] == 1

    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.local_committed_not_pushed

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    rebound = db_session.query(MainAITaskWorktree).filter(MainAITaskWorktree.job_id == new_job.id).one()
    current_sha = subprocess.run(["git", "-C", rebound.path, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert current_sha == local_sha  # the real commit object, unchanged

    generation = _claim_and_run(db_session, superuser_db, owner_id, new_job.id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_count["n"] == 1  # never re-called the AI for already-committed work
    assert push_calls["n"] == 2  # first call crashed; the resumed attempt's real push succeeded
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed
    remote_tip = subprocess.run(
        ["git", "-C", str(patched_github["remote"]), "rev-parse", rebound.branch], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remote_tip == local_sha  # the exact same real commit, genuinely pushed -- not a new one


# =================================================================== Demo 4: dead AFTER push


@pytest.mark.asyncio
async def test_demo_4_dead_after_push_recovers_via_pushed_no_pr_with_founder_approval(db_session, superuser_db, owner_id, patched_github):
    """The worker really pushed the branch to the remote and then died before opening a PR.
    PUSHED_NO_PR -- V0.2's approval gate requires an explicit founder decision before takeover
    continues (the code is already live on GitHub, a real external side effect), and once
    granted, the new attempt must NEVER push a second, redundant commit on top of the real one
    already there."""
    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="repo_edit")
    wt = await create_task_worktree(db_session, task=task, job=dead_job, lease_generation=dead_job.lease_generation, executor_id="dead-worker")
    db_session.commit()
    (Path(wt.path) / "pushed.txt").write_text("real pushed content\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pushed.txt")
    db_session.commit()
    pushed_sha = await push_worktree_branch(wt)
    record_checkpoint(db_session, task=task, goal=goal, job_id=dead_job.id, step="work_result", data={"work_result": {"files": ["pushed.txt"]}})
    db_session.commit()
    _kill_lease(superuser_db, db_session, owner_id, dead_job.id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.pushed_no_pr

    from app.mainai_execution.recovery_approval import RecoveryApprovalRequiredError

    with pytest.raises(RecoveryApprovalRequiredError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")

    grant_recovery_approval(db_session, record=record, approved_by="founder@lifeos.local")
    db_session.commit()

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()
    assert record.status == MainAIRecoveryStatus.completed

    remote_branches_before = subprocess.run(
        ["git", "-C", str(patched_github["remote"]), "log", "--oneline", wt.branch], capture_output=True, text=True, check=True
    ).stdout

    generation = _claim_and_run(db_session, superuser_db, owner_id, new_job.id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    remote_branches_after = subprocess.run(
        ["git", "-C", str(patched_github["remote"]), "log", "--oneline", wt.branch], capture_output=True, text=True, check=True
    ).stdout
    assert remote_branches_after == remote_branches_before  # no second, redundant push

    remote_tip = subprocess.run(
        ["git", "-C", str(patched_github["remote"]), "rev-parse", wt.branch], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remote_tip == pushed_sha
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


# ===================================================================== Demo 5: dead AFTER PR


@pytest.mark.asyncio
async def test_demo_5_dead_after_pr_recovers_via_pr_exists_with_founder_approval(db_session, superuser_db, owner_id, patched_github):
    """The worker pushed AND a PR already exists for the branch (opened by an `open_pr` task,
    or discovered independently) when the worker died. PR_EXISTS -- also gated behind founder
    approval (same real-external-side-effect reasoning as PUSHED_NO_PR, one step further
    along), and the real, already-open PR must never be duplicated."""
    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="repo_edit")
    wt = await create_task_worktree(db_session, task=task, job=dead_job, lease_generation=dead_job.lease_generation, executor_id="dead-worker")
    db_session.commit()
    (Path(wt.path) / "pr.txt").write_text("real content behind a real PR\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="pr.txt")
    db_session.commit()
    await push_worktree_branch(wt)
    patched_github["prs"][f"test-owner:{wt.branch}"] = [{"number": 7, "html_url": "https://example.invalid/pull/7"}]
    record_checkpoint(db_session, task=task, goal=goal, job_id=dead_job.id, step="work_result", data={"work_result": {"files": ["pr.txt"]}})
    db_session.commit()
    _kill_lease(superuser_db, db_session, owner_id, dead_job.id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.pr_exists
    assert record.evidence["pr_numbers"] == [7]

    from app.mainai_execution.recovery_approval import RecoveryApprovalRequiredError

    with pytest.raises(RecoveryApprovalRequiredError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")

    grant_recovery_approval(db_session, record=record, approved_by="founder@lifeos.local")
    db_session.commit()

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()
    assert record.status == MainAIRecoveryStatus.completed
    assert len(patched_github["prs"][f"test-owner:{wt.branch}"]) == 1  # still exactly one PR

    generation = _claim_and_run(db_session, superuser_db, owner_id, new_job.id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


# ============================================================ Demo 6: stale worker returns


@pytest.mark.asyncio
async def test_demo_6_stale_worker_returns_after_takeover_and_is_rejected(db_session, superuser_db, owner_id):
    """REQUIRED demo: the dead worker was not actually dead -- a long GC pause, a slow network
    -- and it wakes up AFTER takeover already superseded its job, and tries to keep acting as
    if it still owned the attempt. Every real write path it touches must reject it, exactly
    like any other stale-lease write V0.1 already fences."""
    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="read_only_audit")
    dead_job_id = dead_job.id
    stale_generation = dead_job.lease_generation
    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.nothing_done
    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()
    assert new_job.id != dead_job_id

    # The stale worker tries to renew its own (now-superseded) lease -- rejected. This is the
    # EXACT gate run_task_execution_job() itself calls before doing any real work or writing
    # any checkpoint (see execution_job.py's renew_mainai_job_lease() calls before each
    # commit) -- so a stale worker that tries to resume its "own" job never even reaches the
    # point of computing new work, let alone writing it.
    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(db_session, dead_job_id, "dead-worker", stale_generation, 120)

    # A second, independent takeover attempt on the SAME (already-superseded) dead job must
    # also be refused -- mark_job_superseded() re-checks under the row lock that the job is
    # still genuinely running with an expired lease before writing anything.
    from app.jobs.mainai_job_lease import JobNotSupersedableError, mark_job_superseded

    with pytest.raises(JobNotSupersedableError):
        mark_job_superseded(superuser_db, job_id=dead_job_id, superseded_by_job_id=new_job.id)
    superuser_db.rollback()

    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "superseded"  # untouched by the stale worker's return


# ======================================================== Demo 7: ambiguous conflicting state


@pytest.mark.asyncio
async def test_demo_7_ambiguous_conflicting_state_fails_closed_no_destructive_recovery(db_session, superuser_db, owner_id, patched_github, tmp_path):
    """REQUIRED demo: the dead worker's local commit and the remote branch tip have genuinely
    diverged (something else pushed different content to the same branch). CONFLICTED_STATE --
    never guessed past, never force-pushed, never silently overwritten. Recovery stops and
    waits for a human; execute_takeover() refuses outright, and nothing about the dead job, the
    task, or either side of the real divergence is touched."""
    task, goal, dead_job = _dispatch_and_claim(db_session, superuser_db, owner_id, task_type="repo_edit")
    dead_job_id = dead_job.id
    wt = await create_task_worktree(db_session, task=task, job=dead_job, lease_generation=dead_job.lease_generation, executor_id="dead-worker")
    db_session.commit()
    (Path(wt.path) / "mine.txt").write_text("the dead worker's own real local commit\n", encoding="utf-8")
    commit_worktree_changes(db_session, wt, message="mine.txt")
    db_session.commit()
    _kill_lease(superuser_db, db_session, owner_id, dead_job_id)

    other_clone = tmp_path / "other-clone"
    remote = patched_github["remote"]
    subprocess.run(["git", "clone", "-q", str(remote), str(other_clone)], check=True)
    subprocess.run(["git", "-C", str(other_clone), "checkout", "-q", "-b", wt.branch, wt.base_sha], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.email", "other@test.local"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "config", "user.name", "Other"], check=True)
    (other_clone / "theirs.txt").write_text("a genuinely different, real remote commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other_clone), "add", "theirs.txt"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "commit", "-q", "-m", "theirs"], check=True)
    subprocess.run(["git", "-C", str(other_clone), "push", "-q", "origin", wt.branch], check=True)
    remote_tip_before = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", wt.branch], capture_output=True, text=True, check=True
    ).stdout.strip()

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.conflicted_state
    assert record.manual_review_required is True
    assert record.status == MainAIRecoveryStatus.classified  # classified, but flagged -- never auto-continued

    with pytest.raises(TakeoverError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")

    # Nothing was touched: no new job, dead job still exactly as it was, remote branch tip
    # exactly as the "other" real commit left it (never overwritten, never force-pushed).
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.running
    assert task.mainai_job_id == dead_job_id
    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "running"  # never superseded -- no takeover ever happened
    remote_tip_after = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", wt.branch], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remote_tip_after == remote_tip_before
