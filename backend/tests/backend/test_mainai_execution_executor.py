"""MainAI Execution Loop V0.1 — executor (app/mainai_execution/executor.py), approval gate
(app/mainai_execution/approval.py), verification (app/mainai_execution/verify.py), and the
task_execution job entry point (app/mainai_execution/execution_job.py).

Covers, in order:
  A. Approval gate enforcement: an approval_required task cannot be dispatched (no mainai_jobs
     row is ever created) until an explicit approval_granted event exists — the founder's
     explicit required test ("executor tries to bypass approval -> stopped by system").
  B. retry_task(): valid retryable_failed -> ready transition; rejected for any other status.
  C. verify_task(): passing/failing targeted_tests, unknown step kind, empty plan.
  D. task_for_job(): the inverse job -> task lookup dispatch_ready_task() sets up.
  E. run_task_execution_job() end to end, real local Postgres + real subprocess pytest runs
     (never faked) with only the LLM provider mocked, matching
     tests/backend/jobs/test_mainai_jobs.py's own corpus_review convention:
       - read_only_audit success path -> job completed, task completed.
       - run_tests verification-failure path -> job completed (the ATTEMPT finished) but task
         retryable_failed, never completed -- and a dependent task is correctly left `pending`
         (not falsely blocked; retryable_failed is not a terminal failure).
       - repo_edit with github_write_enabled left at its default (False) -> real local file
         write + real local verification, but no GitHub network call at all (no fake
         credentials are configured in this test environment, so any accidental network
         attempt would itself fail the test).

Real local Postgres (RLS included). Only the LLM provider is faked, never the DB, RLS, or the
local filesystem/subprocess verification path."""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.jobs import service
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.executor import TaskNotRetryableError
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.verify import VerificationStepError, verify_task
from app.models.mainai_execution import MainAITask, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    """This module's writes span BOTH mainai_tasks/mainai_task_events (app/rls.py's
    apply_mainai_execution_privileges()) AND mainai_jobs/mainai_job_events (the pre-existing
    apply_mainai_job_runtime_privileges()) -- dispatch_ready_task() creates a real mainai_jobs
    row, so both grants must be in place before any test in this module runs, matching the
    same ordering-trap concern test_mainai_jobs.py's and test_mainai_execution_planner.py's own
    identical fixtures each already document for their own narrower slice."""
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


def _goal(db_session, owner_id, *, approval_policy="standard_repo_work"):
    return planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Test goal",
        original_instruction="Do the thing, carefully.",
        created_by="test",
        approval_policy=approval_policy,
    )


def _single_task_plan(db_session, goal, **spec_kwargs) -> MainAITask:
    """Creates a one-task plan and returns that task, already promoted to `ready` by
    create_plan()'s own call to recompute_task_readiness() (it has no dependencies)."""
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "read_only_audit")
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _events(db_session, task_id) -> set[str]:
    rows = db_session.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :id"), {"id": str(task_id)}).all()
    return {row[0] for row in rows}


# ---------------------------------------------------------------- A. approval gate


def test_dispatch_ready_task_stops_an_approval_required_task_with_no_grant_recorded(db_session, superuser_db, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, approval_required=True)

    jobs_before = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}).scalar()

    with pytest.raises(ApprovalRequiredError):
        executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.rollback()

    jobs_after = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}).scalar()
    assert jobs_after == jobs_before == 0

    db_session.refresh(task)
    assert task.status == MainAITaskStatus.ready  # untouched -- the block happened before any state change


def test_dispatch_ready_task_succeeds_once_approval_is_explicitly_granted(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, approval_required=True)

    grant_task_approval(db_session, task=task, approved_by="founder")
    db_session.commit()

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    assert job.job_type == "task_execution"
    assert job.status == MainAIJobStatus.queued
    assert job.input_refs == [{"type": "mainai_task", "id": str(task.id)}]

    db_session.refresh(task)
    assert task.status == MainAITaskStatus.running
    assert task.mainai_job_id == job.id
    assert task.attempts == 1
    assert MainAITaskEventType.dispatched.value in _events(db_session, task.id)


def test_dispatch_ready_task_needs_no_approval_for_a_task_the_policy_marks_auto(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)  # approval_required defaults to False, policy default for read_only_audit is AUTO

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    assert job.status == MainAIJobStatus.queued


def test_dispatch_ready_task_rejects_a_task_that_is_not_ready(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    task.status = MainAITaskStatus.pending
    db_session.flush()

    with pytest.raises(ValueError):
        executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")


# ---------------------------------------------------------------- B. retry_task


def test_retry_task_moves_a_retryable_failed_task_back_to_ready(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    db_session.flush()

    retried = executor.retry_task(db_session, task=task)
    db_session.commit()

    assert retried.status == MainAITaskStatus.ready
    assert MainAITaskEventType.retry_scheduled.value in _events(db_session, task.id)


@pytest.mark.parametrize("status", [MainAITaskStatus.pending, MainAITaskStatus.ready, MainAITaskStatus.running, MainAITaskStatus.completed])
def test_retry_task_rejects_any_non_retryable_status(db_session, owner_id, status):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    task.status = status
    if status == MainAITaskStatus.completed:
        from datetime import datetime

        task.completed_at = datetime.utcnow()
    db_session.flush()

    with pytest.raises(TaskNotRetryableError):
        executor.retry_task(db_session, task=task)


@pytest.mark.parametrize("status", [MainAITaskStatus.pending, MainAITaskStatus.ready, MainAITaskStatus.blocked, MainAITaskStatus.retryable_failed])
def test_cancel_task_cancels_a_not_yet_running_task_and_blocks_its_dependents(db_session, owner_id, status):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="cancel test",
        tasks=[PlannedTaskSpec(description="to be cancelled", task_type="read_only_audit"), PlannedTaskSpec(description="dependent", task_type="read_only_audit", depends_on=[0])],
        created_by="test",
    )
    db_session.commit()
    task, dependent = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    task.status = status
    db_session.flush()

    cancelled = executor.cancel_task(db_session, task=task, cancelled_by="founder", reason="no longer needed")
    db_session.commit()

    assert cancelled.status == MainAITaskStatus.cancelled
    assert cancelled.completed_at is not None
    assert MainAITaskEventType.cancelled.value in _events(db_session, task.id)

    db_session.refresh(dependent)
    assert dependent.status == MainAITaskStatus.blocked  # never falsely left `pending` forever


def test_cancel_task_rejects_a_running_task(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    assert task.status == MainAITaskStatus.running

    with pytest.raises(executor.TaskNotCancellableError):
        executor.cancel_task(db_session, task=task, cancelled_by="founder")


def test_cancel_task_rejects_an_already_terminal_task(db_session, owner_id):
    from datetime import datetime

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    task.status = MainAITaskStatus.completed
    task.completed_at = datetime.utcnow()
    db_session.flush()

    with pytest.raises(executor.TaskNotCancellableError):
        executor.cancel_task(db_session, task=task, cancelled_by="founder")


# ---------------------------------------------------------------- C. verify_task


class _FakeTask:
    def __init__(self, verification_plan):
        self.verification_plan = verification_plan


def test_verify_task_passes_when_the_targeted_test_passes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")

    result = verify_task(_FakeTask([{"kind": "targeted_tests", "target": "tests/test_ok.py"}]), cwd=str(tmp_path))

    assert result.passed is True
    assert len(result.steps) == 1
    assert result.steps[0].passed is True
    assert result.evidence()["passed"] is True


def test_verify_task_fails_when_the_targeted_test_fails(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text("def test_bad():\n    assert False\n")

    result = verify_task(_FakeTask([{"kind": "targeted_tests", "target": "tests/test_bad.py"}]), cwd=str(tmp_path))

    assert result.passed is False
    assert result.steps[0].passed is False
    assert result.steps[0].detail["returncode"] != 0


def test_verify_task_raises_for_an_unknown_step_kind():
    with pytest.raises(VerificationStepError):
        verify_task(_FakeTask([{"kind": "run_a_magic_wand"}]), cwd=".")


def test_verify_task_passes_trivially_for_an_empty_plan():
    result = verify_task(_FakeTask([]), cwd=".")
    assert result.passed is True
    assert result.steps == []


# ---------------------------------------------------------------- D. task_for_job


def test_task_for_job_finds_the_task_a_job_was_dispatched_for(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    found = executor.task_for_job(db_session, job)
    assert found is not None
    assert found.id == task.id


def test_task_for_job_returns_none_for_a_job_with_no_mainai_task_ref(db_session, owner_id):
    job = MainAIJob(owner_id=owner_id, job_type="message_sequence_backfill", status=MainAIJobStatus.queued, input_refs=[], output_refs=[], created_by="test")
    db_session.add(job)
    db_session.flush()

    assert executor.task_for_job(db_session, job) is None


# ---------------------------------------------------------------- E. run_task_execution_job end to end


@pytest.mark.asyncio
async def test_run_task_execution_job_read_only_audit_success_path(db_session, superuser_db, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Analysen visar inga problem."))
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="read_only_audit", verification_plan=[])

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.completed
    assert task.completed_at is not None

    event_types = _events(superuser_db, task.id)
    assert {
        MainAITaskEventType.dispatched.value,
        MainAITaskEventType.verification_started.value,
        MainAITaskEventType.verification_passed.value,
        MainAITaskEventType.completed.value,
    } <= event_types


@pytest.mark.asyncio
async def test_run_task_execution_job_verification_failure_never_completes_the_task(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """Demo-2-in-miniature: the founder's explicit requirement that a task must NEVER become
    `completed` just because the underlying mainai_jobs attempt finished running. A dependent
    task also must not be falsely `blocked` -- retryable_failed is not terminal, so it stays
    `pending`, correctly waiting on a possible retry rather than declaring the branch dead."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_scratch_fail.py").write_text("def test_scratch_fail():\n    assert False\n")

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="failing run_tests + a dependent",
        tasks=[
            PlannedTaskSpec(
                description="run the (deliberately failing) suite",
                task_type="run_tests",
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_scratch_fail.py"}],
            ),
            PlannedTaskSpec(description="depends on the above", task_type="read_only_audit", depends_on=[0]),
        ],
        created_by="test",
    )
    db_session.commit()

    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "run_tests").one()
    dependent = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "read_only_audit").one()
    assert task.status == MainAITaskStatus.ready
    assert dependent.status == MainAITaskStatus.pending

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed  # the ATTEMPT finished running -- this is not "the task succeeded"

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.completed_at is None
    assert task.attempts == 1

    event_types = _events(superuser_db, task.id)
    assert MainAITaskEventType.verification_failed.value in event_types
    assert MainAITaskEventType.completed.value not in event_types

    dependent = superuser_db.get(MainAITask, dependent.id)
    assert dependent.status == MainAITaskStatus.pending  # not falsely blocked; not falsely promoted


@pytest.mark.asyncio
async def test_run_task_execution_job_repo_edit_proposes_only_with_real_local_verification(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """github_write_enabled defaults to False (app/config.py) and no GitHub credentials are
    configured in this test environment, so any accidental attempt at a real network call would
    itself fail this test -- the absence of a mock IS the assertion. What must still be real:
    the AI-proposed file content is actually written locally and a real subprocess pytest run
    verifies it before the task can complete."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat('{"files": [{"path": "backend/tests/test_scratch_edit.py", "content": "def test_scratch_edit():\\n    assert 2 + 3 == 5\\n"}], "commit_message": "Add a passing test"}'),
    )

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(
        db_session,
        goal,
        task_type="repo_edit",
        verification_plan=[{"kind": "targeted_tests", "target": "tests/test_scratch_edit.py"}],
    )

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    written = tmp_path / "backend" / "tests" / "test_scratch_edit.py"
    assert written.exists()
    assert "assert 2 + 3 == 5" in written.read_text()

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.completed

    completed_event = superuser_db.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id AND event_type = 'completed'"), {"id": str(task.id)}
    ).scalar_one()
    assert completed_event["work_result"]["proposed"] is True
    assert "branch" in completed_event["work_result"]


@pytest.mark.asyncio
async def test_run_task_execution_job_run_tests_derives_verification_from_its_own_work_result_not_a_second_pytest_run(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """run_tests' completion is gated by its OWN pytest run's outcome (work_result), never a
    second, redundant subprocess invocation of the same targets via verify_task() -- proven
    here by counting real subprocess.run calls: exactly one per targeted_tests entry, not two
    (verify_task() would otherwise independently re-run the identical target). The failure
    case is the one that actually matters: a failing test run must still correctly land the
    task on retryable_failed, never completed, using only that one real run's result."""
    import subprocess

    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_run_tests_gap.py").write_text("def test_run_tests_gap():\n    assert False\n")

    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(
        db_session,
        goal,
        task_type="run_tests",
        verification_plan=[{"kind": "targeted_tests", "target": "tests/test_run_tests_gap.py"}],
    )

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)

    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    assert call_count["n"] == 1  # not re-run a second time via verify_task()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed  # the test run itself failed -- must never be `completed`
    event_types = _events(superuser_db, task.id)
    assert MainAITaskEventType.completed.value not in event_types
