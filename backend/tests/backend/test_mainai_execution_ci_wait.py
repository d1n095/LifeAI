"""MainAI V0.3 -- CI-wait (app/mainai_execution/ci_wait.py) and the `waiting_ci` wiring into
app/mainai_execution/execution_job.py's `run_task_execution_job()` / `resume_waiting_ci_task()`.

Covers:
  A. evaluate_check_runs(): the pure decision function (no runs, in-progress, all-pass,
     one-failure, unrecognized-conclusion-fails-closed).
  B. start_ci_wait()/poll_ci_wait(): the durable wait record's own lifecycle in isolation.
  C. Demo (real production path): a real `open_pr` task_execution job, dispatched through
     run_task_execution_job() with github_write_enabled -- proves the task lands on
     `waiting_ci` (not `completed`) once a real PR exists, then that a real
     resume_waiting_ci_task() poll (CI green) completes it, or (CI red) sends it to
     `retryable_failed` with `next_retry_at` scheduled, or (deadline passed) times it out.
  D. Security: poll_ci_wait() always polls the wait's OWN durable (repo, sha) -- never
     whatever the branch's current tip happens to be, and refuses to poll if the configured
     repo has drifted from the wait's own repo.

Real local Postgres (RLS included). Only the LLM provider and the GitHub client are faked,
matching test_mainai_execution_executor.py's own convention."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.ci_wait import cancel_ci_wait, evaluate_check_runs, poll_ci_wait, start_ci_wait
from app.mainai_execution.execution_job import resume_waiting_ci_task, run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.mainai_wait import MainAITaskWait, MainAITaskWaitSourceType, MainAITaskWaitStatus
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
    return planner.create_goal(
        db_session, owner_id=owner_id, title="Test goal", original_instruction="Ship it.", created_by="test"
    )


def _single_task_plan(db_session, goal, **spec_kwargs) -> MainAITask:
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "read_only_audit")
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def _events(db_session, task_id) -> set[str]:
    rows = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(task_id)}).all()
    return {row[0] for row in rows}


# ---------------------------------------------------------------- A. evaluate_check_runs()


def test_evaluate_check_runs_no_runs_is_not_done():
    done, passed, evidence = evaluate_check_runs([])
    assert done is False
    assert passed is False
    assert evidence == {"check_run_count": 0}


def test_evaluate_check_runs_still_in_progress_is_not_done():
    done, passed, _ = evaluate_check_runs([{"name": "build", "status": "in_progress", "conclusion": None}])
    assert done is False
    assert passed is False


def test_evaluate_check_runs_all_completed_success_is_done_and_passed():
    done, passed, evidence = evaluate_check_runs(
        [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "neutral"},
        ]
    )
    assert done is True
    assert passed is True
    assert evidence["check_run_count"] == 2


def test_evaluate_check_runs_one_failure_is_done_and_not_passed():
    done, passed, _ = evaluate_check_runs(
        [{"name": "build", "status": "completed", "conclusion": "success"}, {"name": "tests", "status": "completed", "conclusion": "failure"}]
    )
    assert done is True
    assert passed is False


def test_evaluate_check_runs_unrecognized_conclusion_fails_closed():
    """A future GitHub API value this code has never seen must never be silently treated as a
    pass -- fail closed."""
    done, passed, _ = evaluate_check_runs([{"name": "mystery", "status": "completed", "conclusion": "some_brand_new_value"}])
    assert done is True
    assert passed is False


# ---------------------------------------------------------------- B. start_ci_wait()/poll_ci_wait()


def test_start_ci_wait_creates_a_durable_wait_and_moves_the_task_to_waiting_ci(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    db_session.commit()

    assert task.status == MainAITaskStatus.waiting_ci
    assert wait.status == MainAITaskWaitStatus.pending
    assert wait.resource_ref == {"repo": "d1n095/LifeAI", "sha": "abc123"}
    assert wait.job_id == job.id
    assert "wait_started" in _events(db_session, task.id)


@pytest.mark.asyncio
async def test_poll_ci_wait_satisfied_when_all_checks_pass(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_token", "fake-token")

    async def _fake_list_check_runs(self, ref):
        assert ref == "deadbeef"  # exactly the wait's own recorded sha, never anything else
        return [{"name": "ci", "status": "completed", "conclusion": "success"}]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    db_session.commit()

    resolved = await poll_ci_wait(db_session, wait)
    db_session.commit()

    assert resolved.status == MainAITaskWaitStatus.satisfied
    assert resolved.poll_count == 1
    assert resolved.resolved_at is not None


@pytest.mark.asyncio
async def test_poll_ci_wait_failed_when_a_check_fails(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_token", "fake-token")

    async def _fake_list_check_runs(self, ref):
        return [{"name": "ci", "status": "completed", "conclusion": "failure"}]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    db_session.commit()

    resolved = await poll_ci_wait(db_session, wait)
    db_session.commit()

    assert resolved.status == MainAITaskWaitStatus.failed


@pytest.mark.asyncio
async def test_poll_ci_wait_still_pending_reschedules_with_backoff_and_never_exceeds_deadline(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_token", "fake-token")

    async def _fake_list_check_runs(self, ref):
        return []  # no runs yet -- not done

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    wait.deadline_at = datetime.utcnow() + timedelta(seconds=5)  # a short deadline the next poll will already be past
    db_session.commit()

    still_pending = await poll_ci_wait(db_session, wait)
    db_session.commit()
    assert still_pending.status == MainAITaskWaitStatus.pending
    assert still_pending.next_poll_at <= still_pending.deadline_at

    # Force the deadline into the past and poll again -- must time out, never wait forever.
    still_pending.deadline_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()
    timed_out = await poll_ci_wait(db_session, still_pending)
    db_session.commit()
    assert timed_out.status == MainAITaskWaitStatus.timed_out


@pytest.mark.asyncio
async def test_poll_ci_wait_is_a_no_op_on_an_already_terminal_wait(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    wait.status = MainAITaskWaitStatus.satisfied
    wait.resolved_at = datetime.utcnow()
    db_session.commit()

    unchanged = await poll_ci_wait(db_session, wait)
    assert unchanged.poll_count == 0  # never actually polled


def test_cancel_ci_wait_resolves_a_pending_wait(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    db_session.commit()

    cancelled = cancel_ci_wait(db_session, wait, reason="task cancelled")
    db_session.commit()
    assert cancelled.status == MainAITaskWaitStatus.cancelled
    assert cancelled.evidence["cancel_reason"] == "task cancelled"


# ---------------------------------------------------------------- D. security: stale/mismatched repo


@pytest.mark.asyncio
async def test_poll_ci_wait_refuses_to_poll_if_configured_repo_no_longer_matches_the_wait(db_session, owner_id, monkeypatch):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "some-other-owner/some-other-repo")  # drifted since wait creation
    monkeypatch.setattr(settings, "github_token", "fake-token")

    called = {"n": 0}

    async def _fake_list_check_runs(self, ref):
        called["n"] += 1
        return [{"name": "ci", "status": "completed", "conclusion": "success"}]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="deadbeef")
    db_session.commit()

    resolved = await poll_ci_wait(db_session, wait)
    db_session.commit()

    assert called["n"] == 0, "must never poll a mismatched repo"
    assert resolved.status == MainAITaskWaitStatus.pending
    assert "poll_error" in resolved.evidence


# ---------------------------------------------------------------- C. real end-to-end demos


async def _dispatch_open_pr_and_reach_waiting_ci(db_session, superuser_db, owner_id, monkeypatch, *, commit_sha="realcommitsha"):
    """Shared setup for the three real end-to-end demos below: a real repo_edit-completed
    sibling event, github_write_enabled on, a fake (never networked) GitHubClient that creates
    one PR, dispatched through the REAL run_task_execution_job() -- never hand-constructed
    state. Returns (task, job)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")

    async def _fake_create_pull_request(self, *, title, body, head, base):
        return {"number": 7, "html_url": "https://github.com/d1n095/LifeAI/pull/7", "head": f"d1n095:{head}", "base": base, "state": "open"}

    async def _fake_list_pull_requests_for_head(self, *, head, base, state="all"):
        return []

    monkeypatch.setattr(GitHubClient, "create_pull_request", _fake_create_pull_request)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_pull_requests_for_head)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    goal = _goal(db_session, owner_id)
    # Two independent tasks -- the completed `repo_edit` SIBLING's own event is what
    # _find_repo_edit_branch() reads (see execution_job.py); it must never be confused with the
    # `open_pr` task's own event history, which this test asserts on separately below.
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="two tasks",
        tasks=[PlannedTaskSpec(description="edit", task_type="repo_edit"), PlannedTaskSpec(description="open pr", task_type="open_pr")],
        created_by="test",
    )
    db_session.commit()
    repo_edit_task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "repo_edit").one()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "open_pr").one()
    db_session.add(
        MainAITaskEvent(
            task_id=repo_edit_task.id,
            owner_id=owner_id,
            event_type=MainAITaskEventType.completed,
            detail={"work_result": {"branch": "claude/mainai-task-abc", "base_branch": "claude/det-kommer-mer-879lcm", "commit_sha": commit_sha}},
        )
    )
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    db_session.refresh(job)

    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    task = db_session.get(MainAITask, task.id)
    job = db_session.get(MainAIJob, job.id)
    return task, job


@pytest.mark.asyncio
async def test_demo_open_pr_real_dispatch_lands_on_waiting_ci_not_completed(db_session, superuser_db, owner_id, monkeypatch):
    """REQUIRED demo: a real open_pr task, dispatched through the REAL run_task_execution_job()
    path (never a hand-constructed MainAITaskWait), lands on `waiting_ci` -- its own JOB
    reaches `completed` (opening the PR was genuinely this attempt's whole job), but the TASK
    itself is correctly NOT marked completed yet."""
    task, job = await _dispatch_open_pr_and_reach_waiting_ci(db_session, superuser_db, owner_id, monkeypatch)

    assert task.status == MainAITaskStatus.waiting_ci
    assert job.status == MainAIJobStatus.completed
    assert "wait_started" in _events(db_session, task.id)
    assert "completed" not in _events(db_session, task.id)

    wait = db_session.query(MainAITaskWait).filter(MainAITaskWait.task_id == task.id).one()
    assert wait.status == MainAITaskWaitStatus.pending
    assert wait.source_type == MainAITaskWaitSourceType.github_check_runs
    assert wait.resource_ref["sha"] == "realcommitsha"


@pytest.mark.asyncio
async def test_demo_ci_wait_resolves_green_and_completes_the_task(db_session, superuser_db, owner_id, monkeypatch):
    """REQUIRED demo: after the real dispatch above, a real resume_waiting_ci_task() poll
    (mocked GitHub Checks API, real DB state) that finds all checks green moves the task all
    the way to `completed` -- the full CI-wait success path through real production code, not a
    manual shortcut."""
    task, _job = await _dispatch_open_pr_and_reach_waiting_ci(db_session, superuser_db, owner_id, monkeypatch)
    assert task.status == MainAITaskStatus.waiting_ci

    async def _fake_list_check_runs(self, ref):
        assert ref == "realcommitsha"
        return [{"name": "ci", "status": "completed", "conclusion": "success"}]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = db_session.query(MainAITaskWait).filter(MainAITaskWait.task_id == task.id).one()
    await resume_waiting_ci_task(db_session, wait)
    db_session.commit()

    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.completed
    assert task.completed_at is not None
    events = _events(db_session, task.id)
    assert "wait_satisfied" in events
    assert "completed" in events


@pytest.mark.asyncio
async def test_demo_ci_wait_resolves_red_and_schedules_a_backoff_retry(db_session, superuser_db, owner_id, monkeypatch):
    """REQUIRED demo: CI failure sends the task to `retryable_failed` (via the SAME
    _finalize_task_outcome() gate every other task_type's failure goes through) with
    `next_retry_at` scheduled for the automatic retry-with-backoff worker tick -- never silently
    treated as done, never a permanent `failed` while attempts remain."""
    task, _job = await _dispatch_open_pr_and_reach_waiting_ci(db_session, superuser_db, owner_id, monkeypatch)

    async def _fake_list_check_runs(self, ref):
        return [{"name": "ci", "status": "completed", "conclusion": "failure"}]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = db_session.query(MainAITaskWait).filter(MainAITaskWait.task_id == task.id).one()
    await resume_waiting_ci_task(db_session, wait)
    db_session.commit()

    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.next_retry_at is not None
    assert task.next_retry_at > datetime.utcnow()
    assert "wait_failed" in _events(db_session, task.id)


@pytest.mark.asyncio
async def test_demo_ci_wait_times_out_and_never_falsely_completes(db_session, superuser_db, owner_id, monkeypatch):
    """REQUIRED demo: a wait whose deadline has passed with no conclusive check-run result
    never becomes `completed` -- it resolves to retryable_failed/failed via `timed_out`,
    exactly like a real CI outage or a misconfigured repo would need to."""
    task, _job = await _dispatch_open_pr_and_reach_waiting_ci(db_session, superuser_db, owner_id, monkeypatch)

    async def _fake_list_check_runs(self, ref):
        return []  # CI never even started -- this repo/commit is unmonitored

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    wait = db_session.query(MainAITaskWait).filter(MainAITaskWait.task_id == task.id).one()
    wait.deadline_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    await resume_waiting_ci_task(db_session, wait)
    db_session.commit()

    task = db_session.get(MainAITask, task.id)
    assert task.status != MainAITaskStatus.completed
    assert task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)
    assert "wait_timed_out" in _events(db_session, task.id)


@pytest.mark.asyncio
async def test_resume_waiting_ci_task_is_a_defensive_no_op_if_the_task_already_left_waiting_ci(db_session, superuser_db, owner_id, monkeypatch):
    """If a cooperative cancel (or any other path) already moved the task off `waiting_ci`
    before this tick got to it, resume_waiting_ci_task() must not resurrect or override that
    outcome."""
    task, _job = await _dispatch_open_pr_and_reach_waiting_ci(db_session, superuser_db, owner_id, monkeypatch)
    wait = db_session.query(MainAITaskWait).filter(MainAITaskWait.task_id == task.id).one()

    task = db_session.get(MainAITask, task.id)
    task.status = MainAITaskStatus.cancelled
    task.completed_at = datetime.utcnow()
    db_session.commit()

    async def _fake_list_check_runs(self, ref):
        raise AssertionError("must never poll once the task has already left waiting_ci")

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)

    await resume_waiting_ci_task(db_session, wait)
    db_session.commit()

    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.cancelled


# ---------------------------------------------------------------- E. security: concurrent double-finalize


def test_resume_waiting_ci_task_concurrent_same_wait_never_double_finalizes(db_session, superuser_db, owner_id, monkeypatch):
    """Security/correctness attack pass finding: app/worker.py's `_poll_mainai_task_waits`
    picks up due waits with an unlocked `status == pending` query, no lease/claim of its own
    -- two real worker processes could both pick up the SAME due wait and both call
    resume_waiting_ci_task() concurrently. Before this fix, that function read the task with a
    plain db.get() (no row lock), so both callers could pass the `waiting_ci` check and both
    call _finalize_task_outcome() on the same task -- a double-dispatch-shaped race (double
    `completed`/`verification_passed` events, or a double attempts++ / next_retry_at
    overwrite). Fixed by having resume_waiting_ci_task() lock the task row first
    (_lock_task()), same primitive as every other task-status transition in this codebase.
    Real two-thread, two-session pattern, same as
    test_dispatch_ready_task_concurrent_same_task_never_double_dispatches
    (test_mainai_execution_executor.py)."""
    import asyncio
    import threading

    from app.db import SessionLocal

    settings = get_settings()
    monkeypatch.setattr(settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    db_session.commit()
    task_id = task.id
    wait_id = wait.id

    async def _fake_list_check_runs(self, ref):
        return [{"name": "ci", "status": "completed", "conclusion": "success"}]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _fake_list_check_runs)
    monkeypatch.setattr(GitHubClient, "is_configured", lambda self: True)

    errors: list[str] = []
    barrier = threading.Barrier(2, timeout=5)

    def _worker():
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            w = session.get(MainAITaskWait, wait_id)
            barrier.wait()
            asyncio.run(resume_waiting_ci_task(session, w))
            session.commit()
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below, not swallowed
            session.rollback()
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_worker), threading.Thread(target=_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"both callers should complete cleanly (the loser is a no-op, not an error): {errors}"

    final_task = superuser_db.get(MainAITask, task_id)
    assert final_task.status == MainAITaskStatus.completed
    assert final_task.attempts == 1, "attempts must not be double-touched by the race"

    completed_events = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type = 'completed'"), {"t": str(task_id)}
    ).scalar()
    assert completed_events == 1, "exactly one 'completed' event, not one per racing caller"

    wait_satisfied_events = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type = 'wait_satisfied'"), {"t": str(task_id)}
    ).scalar()
    assert wait_satisfied_events == 1, "exactly one 'wait_satisfied' event, not one per racing caller"
