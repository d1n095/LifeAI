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
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.integrations.github_client import GitHubClientError
from app.jobs import service
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, lessons, planner
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.executor import TaskNotRetryableError
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.verify import VerificationStepError, verify_task
from app.models.mainai_execution import EngineeringLessonSeverity, MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
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


# ---------------------------------------------------------------- B2. concurrency (hardening pass)


def test_dispatch_ready_task_concurrent_same_task_never_double_dispatches(db_session, superuser_db, owner_id):
    """Hardening pass finding (P1): the original dispatch_ready_task() read `task.status` in
    Python and then unconditionally mutated it with no locking -- two real callers racing the
    SAME `ready` task (e.g. two auto-advance ticks in two worker processes) could both pass the
    in-memory check and both create a `mainai_jobs` row / `dispatched` event / attempts++ for
    what must be exactly one real dispatch. Fixed by `_lock_task()` (`SELECT ... FOR UPDATE` +
    `populate_existing=True`) as the first line of dispatch_ready_task() -- the loser blocks on
    the row lock until the winner commits, then re-checks status against the FRESH row and
    cleanly raises ValueError instead of creating a second, orphaned job. Same real-thread,
    real-two-session pattern as test_mainai_jobs.py's
    test_create_job_concurrent_same_owner_and_key_is_race_safe."""
    import threading

    from app.db import SessionLocal

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    task_id = task.id
    goal_id = goal.id

    results: list[uuid.UUID] = []
    errors: list[str] = []
    barrier = threading.Barrier(2, timeout=5)

    def _worker():
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            t = session.get(MainAITask, task_id)
            g = session.get(type(goal), goal_id)
            barrier.wait()
            job = executor.dispatch_ready_task(session, task=t, goal=g, dispatched_by="race-worker")
            session.commit()
            results.append(job.id)
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

    assert len(results) == 1, f"exactly one caller must win the dispatch, got results={results} errors={errors}"
    assert len(errors) == 1 and "not 'ready'" in errors[0], f"the loser must cleanly raise ValueError, got: {errors}"

    job_count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}).scalar()
    assert job_count == 1, "no orphaned second mainai_jobs row from the losing caller"

    dispatched_events = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type = 'dispatched'"), {"t": str(task_id)}
    ).scalar()
    assert dispatched_events == 1, "exactly one 'dispatched' event, not one per racing caller"

    final_task = superuser_db.get(MainAITask, task_id)
    assert final_task.attempts == 1, "attempts must not be double-incremented by the race"
    assert final_task.status == MainAITaskStatus.running


def test_cancel_task_concurrent_with_dispatch_never_produces_a_contradictory_state(db_session, superuser_db, owner_id):
    """Hardening pass finding (P1), same root cause as the dispatch/dispatch race above but
    between two DIFFERENT transitions: a founder cancelling a task at the exact moment an
    auto-advance tick dispatches it. Without `_lock_task()` in cancel_task() too, both could
    succeed in-memory and leave the task simultaneously `cancelled` (dependents blocked) and
    `running` (a real mainai_jobs row in flight) depending on write order -- a state no code
    path is designed to reconcile. With the lock, whichever transaction commits first wins and
    the second cleanly raises (TaskNotCancellableError or ValueError) against the fresh row."""
    import threading

    from app.db import SessionLocal

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    task_id = task.id
    goal_id = goal.id

    outcomes: list[str] = []
    barrier = threading.Barrier(2, timeout=5)

    def _dispatch():
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            t = session.get(MainAITask, task_id)
            g = session.get(type(goal), goal_id)
            barrier.wait()
            executor.dispatch_ready_task(session, task=t, goal=g, dispatched_by="race-worker")
            session.commit()
            outcomes.append("dispatch_ok")
        except Exception:  # noqa: BLE001 - either outcome is acceptable, contradiction is not
            session.rollback()
            outcomes.append("dispatch_failed")
        finally:
            session.close()

    def _cancel():
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            t = session.get(MainAITask, task_id)
            barrier.wait()
            executor.cancel_task(session, task=t, cancelled_by="founder")
            session.commit()
            outcomes.append("cancel_ok")
        except Exception:  # noqa: BLE001 - either outcome is acceptable, contradiction is not
            session.rollback()
            outcomes.append("cancel_failed")
        finally:
            session.close()

    threads = [threading.Thread(target=_dispatch), threading.Thread(target=_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert set(outcomes) in ({"dispatch_ok", "cancel_failed"}, {"cancel_ok", "dispatch_failed"}), (
        f"exactly one side must win, never both: {outcomes}"
    )

    final_task = superuser_db.get(MainAITask, task_id)
    if "dispatch_ok" in outcomes:
        assert final_task.status == MainAITaskStatus.running
        job_count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}).scalar()
        assert job_count == 1
    else:
        assert final_task.status == MainAITaskStatus.cancelled
        job_count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}).scalar()
        assert job_count == 0, "the cancel winner must mean NO job was ever created for this task"


def test_record_engineering_lesson_for_the_dispatch_race_and_commit_ordering_fix(db_session, owner_id):
    """MAINAI V0.1 hardening pass (post-PR #57): engineering learning loop, mandatory for a
    P1 finding. Root cause was TWO-LAYERED and only the second layer showed up under real
    concurrency testing (not by inspection) -- see this module's own two concurrent tests
    above and dispatch_ready_task()'s own docstring for the full account:

      1. dispatch_ready_task()/retry_task()/cancel_task() checked `task.status` in Python and
         then unconditionally mutated it, with no row lock -- two real callers racing the same
         task could both pass the check.
      2. Locking alone was NOT enough: mainai_jobs_service.create_job() ends with a real
         `db.commit()` (needed for its own SAVEPOINT-based idempotency to be safe), and calling
         it in the MIDDLE of dispatch_ready_task() released the task's row lock mid-critical-
         section AND left a real, durable job with no task-side state (status/attempts/event)
         if the process crashed in the gap right after.

    Fixed by (a) `_lock_task()` -- SELECT ... FOR UPDATE with populate_existing=True -- as the
    first statement of every task-state transition, and (b) reordering dispatch_ready_task() so
    every task-side write that decides "can this task be dispatched again" happens BEFORE
    create_job() is called, using a pre-generated job_id, so create_job()'s own commit is the
    ONE atomic commit for the whole race-critical operation."""
    lesson = lessons.record_lesson(
        db_session,
        problem=(
            "Two concurrent callers of dispatch_ready_task() (e.g. two auto-advance ticks in "
            "different worker processes, or a founder double-clicking an API action) racing "
            "the SAME `ready` MainAITask could both pass the in-memory status check and both "
            "dispatch it -- duplicate mainai_jobs rows / duplicate 'dispatched' events / a lost "
            "update between cancel and dispatch. A row lock alone did not fully close this: "
            "mainai_jobs_service.create_job() ends with a real db.commit(), so calling it "
            "mid-critical-section released the lock AND left a real job durably created before "
            "the task's own status/attempts/event existed -- a crash in that gap would leave a "
            "job nothing owns."
        ),
        root_cause=(
            "Task-state transitions read `task.status` and then unconditionally mutated it with "
            "no locking. Separately, the shared create_job() helper's own atomicity contract "
            "(a full commit at the end, required for its SAVEPOINT-based idempotency to be "
            "safe under real concurrency) was violated by a caller that invoked it in the "
            "middle of a larger multi-row operation instead of last."
        ),
        affected_component="app.mainai_execution.executor / app.jobs.service.create_job",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Found and proven by real two-thread, two-session concurrency tests against local "
            "Postgres during the V0.1 hardening pass, not by code inspection -- the first fix "
            "attempt (row lock only, task mutations after create_job()) still failed "
            "test_dispatch_ready_task_concurrent_same_task_never_double_dispatches with both "
            "callers returning the SAME job id."
        ),
        fix=(
            "_lock_task() (SELECT ... FOR UPDATE + populate_existing=True) as the first "
            "statement of dispatch_ready_task()/retry_task()/cancel_task(). "
            "dispatch_ready_task() now writes status/started_at/attempts/the dispatched event "
            "BEFORE calling create_job(), using a pre-generated job_id (create_job() now "
            "accepts one explicitly for exactly this reason) -- create_job()'s own commit is "
            "the ONE atomic commit for the whole operation, holding the lock continuously "
            "until it. task.mainai_job_id (a real FK, so it cannot be set before the job row "
            "exists) is written in a second statement right after, safe without a lock because "
            "task.status is already durably 'running' by then."
        ),
        general_rule=(
            "A shared helper that ends with its own commit (create_job(), or any function with "
            "the same SAVEPOINT+commit idempotency shape) can NEVER be called in the middle of "
            "another function's row-locked critical section -- its commit silently ends that "
            "transaction and releases the lock early. Reorder so every write that must be "
            "atomic with the helper's own effect happens BEFORE it, and treat the helper's "
            "return as the last step, not a middle one."
        ),
        applies_to=["task_execution", "mainai_jobs", "dispatch_ready_task", "row_locking", "create_job"],
        source_type="branch_registry_pass",
        source_ref="MAINAI V0.1 hardening pass (post-PR #57) -- dispatch/retry/cancel concurrency + commit-ordering fix",
        created_by="hardening-pass",
        first_seen_at=datetime.utcnow(),
        regression_test=(
            "tests/backend/test_mainai_execution_executor.py::"
            "test_dispatch_ready_task_concurrent_same_task_never_double_dispatches and "
            "test_cancel_task_concurrent_with_dispatch_never_produces_a_contradictory_state"
        ),
    )
    db_session.commit()

    assert lesson.id is not None
    assert lesson.severity == EngineeringLessonSeverity.high
    assert lesson.source_type == "branch_registry_pass"

    found = lessons.lookup_lessons(db_session, applies_to_any=["task_execution"])
    assert any(item.id == lesson.id for item in found)


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


def test_verify_task_treats_a_subprocess_timeout_as_a_failed_step_not_an_uncaught_exception(tmp_path):
    """Hardening pass finding (P1): subprocess.run(..., timeout=...) raises TimeoutExpired, a
    THIRD exception shape neither VerificationStepError (malformed plan) nor a normal
    passed/failed VerificationStepResult. Before the fix, this propagated straight out of
    verify_task() past every except clause built for it -- run_task_execution_job()'s own
    `except (TaskExecutionError, VerificationStepError, GitHubClientError)` does not include
    it -- landing at app/worker.py's generic handler, which can mark the mainai_jobs row failed
    but has no way to reach the MainAITask itself. The task was left stuck at `running`
    forever: not retryable, not cancellable (see executor.py's cancel_task() docstring), only
    ever visible as TaskLiveness.dead with nothing acting on that classification. A timeout
    must be an ordinary failed VerificationStepResult, exactly like a real assertion failure."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(5)\n")

    result = verify_task(_FakeTask([{"kind": "targeted_tests", "target": "tests/test_slow.py", "timeout_seconds": 1}]), cwd=str(tmp_path))

    assert result.passed is False
    assert result.steps[0].passed is False
    assert result.steps[0].detail["error"] == "timeout"


def test_run_pytest_treats_a_subprocess_timeout_as_a_failed_result_not_an_uncaught_exception(tmp_path):
    """Same fix, sibling call site: execution_job.py's _run_pytest() is run_tests' own PRIMARY
    work (not verification), used directly by _handle_run_tests() -- had the identical
    TimeoutExpired gap."""
    from app.mainai_execution.execution_job import _run_pytest

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(5)\n")

    result = _run_pytest("tests/test_slow.py", cwd=tmp_path, timeout_seconds=1)

    assert result["passed"] is False
    assert result["error"] == "timeout"


@pytest.mark.asyncio
async def test_run_task_execution_job_run_tests_with_a_subprocess_timeout_never_leaves_the_task_stuck_running(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """The full end-to-end proof, mutation-test style: reproduces the pre-fix bug directly by
    calling the REAL, unmocked timeout path through the full run_task_execution_job() job entry
    point (a real subprocess, a real 1-second timeout, no faking) and confirms the task reaches
    retryable_failed -- never left at `running` (which is what happened before this fix)."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(5)\n")
    # _handle_run_tests() doesn't forward a step's own 'timeout_seconds' to _run_pytest() (it
    # always uses the 300s default) -- force a 1s timeout here so the test doesn't have to wait
    # 300s for the real default to trigger. Not itself the bug under test here.
    real_run_pytest = execution_job._run_pytest
    monkeypatch.setattr(execution_job, "_run_pytest", lambda target, *, cwd, timeout_seconds=300: real_run_pytest(target, cwd=cwd, timeout_seconds=1))

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(
        db_session,
        goal,
        task_type="run_tests",
        verification_plan=[{"kind": "targeted_tests", "target": "tests/test_slow.py"}],
    )

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed, "a subprocess timeout must never leave the task stuck at running"
    assert job.status == MainAIJobStatus.completed


def test_record_engineering_lesson_for_the_subprocess_timeout_task_stuck_running_fix(db_session, owner_id):
    lesson = lessons.record_lesson(
        db_session,
        problem=(
            "subprocess.run(..., timeout=...) raises subprocess.TimeoutExpired, which was not "
            "caught anywhere between the two real pytest subprocess call sites "
            "(_run_targeted_tests() in verify.py, _run_pytest() in execution_job.py) and "
            "run_task_execution_job()'s own except tuple (TaskExecutionError, "
            "VerificationStepError, GitHubClientError). A timed-out subprocess propagated all "
            "the way to app/worker.py's generic `except Exception`, which marks the mainai_jobs "
            "row failed but has no way to reach the MainAITask -- _finalize_task_outcome() is "
            "only ever called from inside run_task_execution_job()'s own try/except. The task "
            "was left stuck at `running` forever: not retryable (retry_task() rejects "
            "non-retryable statuses), not cancellable (cancel_task() deliberately excludes "
            "`running`), correctly classified as TaskLiveness.dead by liveness.py but with "
            "nothing downstream ever acting on that classification by design."
        ),
        root_cause=(
            "Two subprocess call sites each had a bare `subprocess.run(..., timeout=...)` with "
            "no try/except for the one exception type a timeout specifically raises -- both "
            "sites already correctly handled a non-zero returncode (an ordinary test failure) "
            "but neither handled the DIFFERENT exception-shaped failure mode a timeout produces."
        ),
        affected_component="app.mainai_execution.verify._run_targeted_tests, app.mainai_execution.execution_job._run_pytest",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Reproduced end to end with a real subprocess (a test file that sleeps 5s against a "
            "1s timeout) through the full run_task_execution_job() entry point -- pre-fix, this "
            "raised uncaught; post-fix, the task cleanly reaches retryable_failed."
        ),
        fix=(
            "Both call sites now catch subprocess.TimeoutExpired and return an ordinary FAILED "
            "result (VerificationStepResult(passed=False, ...) / {'passed': False, ...}) "
            "instead of letting the exception propagate -- routing a timeout through the exact "
            "same attempts-remaining retry/failed logic every other test failure already uses."
        ),
        general_rule=(
            "Every real subprocess call with a timeout= argument needs an explicit "
            "except subprocess.TimeoutExpired alongside its normal returncode handling -- a "
            "timeout is a DIFFERENT failure shape (an exception) than an ordinary failure (a "
            "non-zero returncode), and any except tuple built by enumerating 'the exceptions "
            "this code path can raise' from the ordinary-failure case alone will miss it. This "
            "applies to every subprocess.run(..., timeout=...) call in the codebase, not just "
            "these two."
        ),
        applies_to=["subprocess", "timeout", "run_tests", "verification", "task_stuck_running", "subprocess_repo_edit_attack"],
        source_type="branch_registry_pass",
        source_ref="MAINAI V0.1 hardening pass (post-PR #57) -- subprocess/repo_edit attack, timeout/kill handling",
        created_by="hardening-pass",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_executor.py::test_run_task_execution_job_run_tests_with_a_subprocess_timeout_never_leaves_the_task_stuck_running",
    )
    db_session.commit()

    assert lesson.id is not None
    found = lessons.lookup_lessons(db_session, applies_to_any=["task_stuck_running"])
    assert any(item.id == lesson.id for item in found)


def test_verify_task_raises_for_an_unknown_step_kind():
    with pytest.raises(VerificationStepError):
        verify_task(_FakeTask([{"kind": "run_a_magic_wand"}]), cwd=".")


def test_verify_task_passes_trivially_for_an_empty_plan():
    result = verify_task(_FakeTask([]), cwd=".")
    assert result.passed is True
    assert result.steps == []


@pytest.mark.parametrize("bad_target", ["../../../etc/passwd", "tests/../../secrets.py", "/etc/passwd", "..", ""])
def test_verify_task_rejects_a_path_traversing_or_absolute_target(tmp_path, bad_target):
    """Hardening pass finding (P1), second layer of defense: even if an unsafe target somehow
    reached execution without ever passing through planner.create_plan()'s own rejection (see
    tests/backend/test_mainai_execution_planner.py), verify_task() -- the actual subprocess
    boundary -- refuses it independently, never silently running `python -m pytest` against a
    path outside `cwd`."""
    with pytest.raises(VerificationStepError):
        verify_task(_FakeTask([{"kind": "targeted_tests", "target": bad_target}]), cwd=str(tmp_path))


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
async def test_mark_completed_called_twice_for_the_same_lease_generation_never_produces_a_duplicate_terminal_event(
    db_session, superuser_db, owner_id, monkeypatch
):
    """State-machine mutation matrix (hardening pass): 'duplicate terminal events'. Simulates
    two racing runners believing they both still hold the SAME job lease (worker_id AND
    lease_generation identical) after run_task_execution_job() has already legitimately
    finalized the task -- e.g. a worker process that stalled right after its own mark_completed
    call landed, then resumed and replayed the same finalization logic. _guarded_job_write()'s
    fencing WHERE clause (`... AND lease_generation = :lease_generation AND status = 'running'`)
    is a compare-and-swap on job status, not just identity: once the first call has already
    moved status away from 'running', a second call with the IDENTICAL (worker_id,
    lease_generation) still finds zero matching rows and must raise JobLeaseLostError -- proving
    a second _finalize_task_outcome() call (which is only ever reached after mark_completed
    succeeds -- see run_task_execution_job()) is structurally unreachable, so exactly one
    `completed` MainAITaskEvent can ever be recorded for a single real attempt."""
    from app.jobs.mainai_job_lease import JobLeaseLostError

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

    with pytest.raises(JobLeaseLostError):
        service.mark_completed(superuser_db, job, worker_id="worker-1", lease_generation=generation)
    superuser_db.rollback()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.completed
    completed_events = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :id AND event_type = 'completed'"), {"id": str(task.id)}
    ).scalar()
    assert completed_events == 1


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


# ---------------------------------------------------------------- E'. repo_edit absolute-path attack
#
# Hardening pass finding (P0): pathlib's `/` operator silently DISCARDS the left operand when
# the right operand is absolute -- `repo_root / "/etc/cron.d/x" == Path("/etc/cron.d/x")`, not
# an error. The pre-fix `_parse_code_agent_response()` only rejected '..' in an AI-proposed
# file path, never an absolute one, making a repo_edit task's code-agent response an arbitrary-
# file-WRITE primitive onto the executor host's filesystem. See
# app/mainai_execution/execution_job.py's `_validate_repo_edit_file_path()` for the fix.


def test_parse_code_agent_response_rejects_an_absolute_file_path(db_session):
    from app.mainai_execution.execution_job import TaskExecutionError, _parse_code_agent_response

    raw = '{"files": [{"path": "/etc/cron.d/mainai-pwned", "content": "* * * * * root touch /tmp/pwned"}], "commit_message": "x"}'
    with pytest.raises(TaskExecutionError, match="absolute"):
        _parse_code_agent_response(raw)


def test_parse_code_agent_response_still_accepts_a_safe_relative_path(db_session):
    from app.mainai_execution.execution_job import _parse_code_agent_response

    raw = '{"files": [{"path": "backend/app/x.py", "content": "x = 1\\n"}], "commit_message": "x"}'
    parsed = _parse_code_agent_response(raw)
    assert parsed["files"][0]["path"] == "backend/app/x.py"


@pytest.mark.asyncio
async def test_run_task_execution_job_repo_edit_with_an_absolute_ai_proposed_path_never_writes_outside_the_repo_and_never_completes(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """The full end-to-end proof: a code-agent response proposing an absolute path must not
    write ANYTHING at that path, and the task must land on retryable_failed (attempts remain),
    never completed -- matching how every other TaskExecutionError-raising handler failure is
    already treated (see test_run_task_execution_job_verification_failure_never_completes_the_task
    for the sibling verification-failure case)."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    victim = tmp_path.parent / f"mainai_absolute_write_poc_{uuid.uuid4().hex}.txt"
    assert not victim.exists()
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat('{"files": [{"path": "' + str(victim) + '", "content": "PWNED"}], "commit_message": "x"}'),
    )

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="repo_edit", verification_plan=[])

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    try:
        assert not victim.exists(), "the absolute-path write must never have happened"
    finally:
        if victim.exists():
            victim.unlink()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.completed_at is None


@pytest.mark.asyncio
async def test_run_task_execution_job_repo_edit_with_a_symlink_escape_never_writes_outside_the_repo(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """Defense in depth: even a syntactically clean relative path (no '..', not absolute) must
    not be able to escape repo_root via a symlink already present in the checked-out tree --
    _handle_repo_edit()'s resolve()-based confinement check (beyond
    _validate_repo_edit_file_path()'s own syntactic checks) is what catches this."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    outside = tmp_path.parent / f"mainai_symlink_escape_target_{uuid.uuid4().hex}"
    outside.mkdir()
    (tmp_path / "escape_hatch").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat('{"files": [{"path": "escape_hatch/pwned.txt", "content": "PWNED"}], "commit_message": "x"}'),
    )

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="repo_edit", verification_plan=[])

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    try:
        assert not (outside / "pwned.txt").exists(), "the symlink-escape write must never have happened"
    finally:
        for p in outside.glob("*"):
            p.unlink()
        outside.rmdir()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed


@pytest.mark.asyncio
async def test_mutation_disabling_the_syntactic_path_guard_alone_is_still_caught_by_the_resolve_confinement_check(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """Mutation test: proves the two guards are genuinely INDEPENDENT layers, not one guard
    dressed up as two. With _validate_repo_edit_file_path() disabled entirely (simulating that
    guard being accidentally removed or broken in a future change), the absolute path still
    reaches _handle_repo_edit() -- and its own separate resolve()-and-compare confinement check
    must still block the write on its own."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(execution_job, "_validate_repo_edit_file_path", lambda path_value: path_value)  # mutation: guard #1 disabled
    victim = tmp_path.parent / f"mainai_mutation_poc_{uuid.uuid4().hex}.txt"
    assert not victim.exists()
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat('{"files": [{"path": "' + str(victim) + '", "content": "PWNED"}], "commit_message": "x"}'),
    )

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="repo_edit", verification_plan=[])

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    try:
        assert not victim.exists(), "guard #2 (resolve-confinement in _handle_repo_edit) must independently block this even with guard #1 disabled"
    finally:
        if victim.exists():
            victim.unlink()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed


def test_record_engineering_lesson_for_the_repo_edit_absolute_path_write_fix(db_session, owner_id):
    lesson = lessons.record_lesson(
        db_session,
        problem=(
            "_handle_repo_edit()'s file-write path only rejected '..' in an AI-proposed file "
            "path, never an absolute one. pathlib's `/` operator silently DISCARDS the left "
            "operand when the right operand is absolute -- `repo_root / '/etc/cron.d/x'` "
            "returns exactly '/etc/cron.d/x', not an error -- so a hallucinated or prompt-"
            "injected absolute path in a repo_edit task's code-agent response was an arbitrary-"
            "file-WRITE primitive onto the executor host's filesystem, wherever the mainai "
            "worker process has write access."
        ),
        root_cause=(
            "verify.py's validate_targeted_tests_target() (a READ/subprocess-argv path) had "
            "already been given both the '..' AND absolute-path checks earlier in this "
            "hardening pass, and its own docstring incorrectly asserted the WRITE path in "
            "_parse_code_agent_response() already matched -- it did not; only the '..' check "
            "existed there. Fixing one call site's path-validation gap does not verify a "
            "sibling call site actually has the same protection -- it has to be checked "
            "directly, the same generalization gap already named in the crash-matrix point I "
            "engineering lesson."
        ),
        affected_component="app.mainai_execution.execution_job._handle_repo_edit",
        severity=EngineeringLessonSeverity.critical,
        evidence=(
            "Reproduced directly: _parse_code_agent_response() accepted a file entry with "
            "path='/tmp/...' before the fix, and Path('/repo/root') / '/etc/cron.d/x' was "
            "confirmed to evaluate to Path('/etc/cron.d/x') via the Python pathlib docs' own "
            "documented (not a bug) semantics."
        ),
        fix=(
            "Added _validate_repo_edit_file_path() (rejects absolute AND '..' paths, mirroring "
            "validate_targeted_tests_target()) at parse time, PLUS a second, independent "
            "resolve()-and-compare confinement check in _handle_repo_edit() itself for defense "
            "in depth against symlink escapes -- proven independent via a mutation test that "
            "disables the first guard alone and confirms the second still blocks the write."
        ),
        general_rule=(
            "Any code path that joins a caller-controlled path segment onto a trusted base "
            "directory with pathlib's `/` operator must explicitly reject absolute segments -- "
            "the operator's own semantics silently discard the base for an absolute right-hand "
            "side, which is very unlikely to be what the author intended and is not visible "
            "from reading the join expression alone. Grep every `_root / <ai_or_user_input>` "
            "or `base_dir / <external_value>` join site in the codebase for this exact shape."
        ),
        applies_to=["repo_edit", "path_traversal", "arbitrary_file_write", "subprocess_repo_edit_attack"],
        source_type="branch_registry_pass",
        source_ref="MAINAI V0.1 hardening pass (post-PR #57) -- subprocess/repo_edit attack, absolute-path arbitrary write",
        created_by="hardening-pass",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_executor.py::test_run_task_execution_job_repo_edit_with_an_absolute_ai_proposed_path_never_writes_outside_the_repo_and_never_completes",
    )
    db_session.commit()

    assert lesson.id is not None
    found = lessons.lookup_lessons(db_session, applies_to_any=["arbitrary_file_write"])
    assert any(item.id == lesson.id for item in found)


@pytest.mark.asyncio
async def test_finalize_repo_edit_resumes_correctly_when_the_branch_already_exists_from_a_prior_uncheckpointed_push(db_session, owner_id, monkeypatch):
    """Crash matrix finding H (hardening pass): run_task_execution_job() checkpoints
    _finalize_repo_edit()'s result under step='finalized' specifically so a crash AFTER a real
    GitHub push succeeds but BEFORE that checkpoint commits doesn't push a second time -- but on
    resume (no 'finalized' checkpoint found), _finalize_repo_edit() used to run again from
    scratch: re-read base_sha from base_branch (unchanged by the first push), see the task
    branch already existed and swallow that as 'fine, commit on top' -- then build the new
    commit on the STALE base_sha instead of the branch's actual current tip. GitHub's real
    fast-forward-only update_ref() correctly rejected that non-fast-forward update, and the
    task ended up permanently `failed` even though the first attempt's push had already
    durably succeeded. Reproduced here with a stateful fake GitHubClient modeling exactly that:
    call _finalize_repo_edit() twice for the SAME task/work_result (simulating dispatch, then a
    crash before the checkpoint, then a resume) -- both calls must succeed, and the second must
    build on the first's real result, never on the stale original base."""
    from app.integrations.github_client import GitHubClient
    from app.mainai_execution.execution_job import _finalize_repo_edit

    settings = get_settings()  # @lru_cache'd singleton -- the same object execution_job.py's own get_settings() call returns
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")

    refs: dict[str, str] = {"claude/det-kommer-mer-879lcm": "base-sha-0"}
    commit_parents: dict[str, str] = {"base-sha-0": None}
    calls: list[str] = []

    async def _fake_get_ref(self, branch):
        calls.append(f"get_ref:{branch}")
        if branch not in refs:
            raise GitHubClientError(f"404: no such branch {branch}")
        return refs[branch]

    async def _fake_create_branch(self, *, new_branch, from_sha):
        calls.append(f"create_branch:{new_branch}:{from_sha}")
        if new_branch in refs:
            raise GitHubClientError("422: Reference already exists")
        refs[new_branch] = from_sha

    async def _fake_commit_multiple_files(self, *, branch, base_sha, files, message):
        calls.append(f"commit:{branch}:{base_sha}")
        current_tip = refs.get(branch)
        if current_tip is not None and current_tip != base_sha:
            # The real, fast-forward-only failure this test exists to prove never happens on
            # a correct resume: the new commit's parent (base_sha) is not the branch's actual
            # current HEAD.
            raise GitHubClientError("422: Update is not a fast forward")
        new_sha = f"commit-{len(commit_parents)}"
        commit_parents[new_sha] = base_sha
        refs[branch] = new_sha
        return {"commit_sha": new_sha, "tree_sha": f"tree-{new_sha}", "blob_shas": {f["path"]: f"blob-{new_sha}" for f in files}}

    monkeypatch.setattr(GitHubClient, "get_ref", _fake_get_ref)
    monkeypatch.setattr(GitHubClient, "create_branch", _fake_create_branch)
    monkeypatch.setattr(GitHubClient, "commit_multiple_files", _fake_commit_multiple_files)

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="repo_edit")
    work_result = {"files": [{"path": "backend/app/x.py", "content": "x = 1\n"}], "commit_message": "test edit"}

    first = await _finalize_repo_edit(db_session, task, work_result)
    assert first["proposed"] is False
    branch = first["branch"]
    assert refs[branch] == first["commit_sha"]

    # Simulates the crash: no checkpoint was recorded for `first`'s result, so
    # run_task_execution_job() would call _finalize_repo_edit() again on resume.
    second = await _finalize_repo_edit(db_session, task, work_result)
    assert second["proposed"] is False
    assert second["commit_sha"] != first["commit_sha"], "a real second commit is created (not perfectly idempotent, but must succeed)"
    assert refs[branch] == second["commit_sha"]
    # The critical assertion: the second commit's parent is the FIRST commit (a true
    # fast-forward), never the original, stale base_branch sha.
    assert commit_parents[second["commit_sha"]] == first["commit_sha"]


@pytest.mark.asyncio
async def test_handle_open_pr_resumes_correctly_without_creating_a_duplicate_pr(db_session, owner_id, monkeypatch):
    """Crash matrix finding I (hardening pass): unlike repo_edit, open_pr's real PR creation
    happens INSIDE the same 'work_result' computation run_task_execution_job() checkpoints --
    there is no separate 'finalized' checkpoint. A crash between a successful
    create_pull_request() call and that checkpoint's commit meant resume called
    _handle_open_pr() again from scratch, and create_pull_request() is not naturally
    idempotent (GitHub rejects a second open PR for the same head/base with a 422) -- the
    task would land on retryable_failed/failed even though the first PR had already durably
    succeeded. Reproduced here by calling _handle_open_pr() twice for the same
    task/goal/completed-repo_edit-sibling (simulating dispatch, then a crash before the
    checkpoint, then a resume): both calls must succeed, and only ONE real
    create_pull_request() call may ever happen -- the second call must find and reuse the
    existing PR via list_pull_requests_for_head()."""
    from app.integrations.github_client import GitHubClient
    from app.mainai_execution.execution_job import _handle_open_pr

    settings = get_settings()
    monkeypatch.setattr(settings, "github_write_enabled", True)
    monkeypatch.setattr(settings, "github_token", "fake-token-never-real")
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")

    created_prs: list[dict] = []
    create_calls = {"n": 0}

    async def _fake_create_pull_request(self, *, title, body, head, base):
        # Mirrors a real GitHub quirk this fix relies on: create_pull_request()'s own `head`
        # is a bare branch name (same-repo PR), but the pulls-LIST endpoint's `head` filter
        # requires the owner-prefixed `owner:branch` form -- so the stored/comparable value
        # here is intentionally owner-prefixed even though the create call itself wasn't.
        create_calls["n"] += 1
        pr = {"number": 42, "html_url": "https://github.com/d1n095/LifeAI/pull/42", "head": f"d1n095:{head}", "base": base, "state": "open"}
        created_prs.append(pr)
        return pr

    async def _fake_list_pull_requests_for_head(self, *, head, base, state="all"):
        return [pr for pr in created_prs if pr["head"] == head and pr["base"] == base and (state == "all" or pr["state"] == state)]

    monkeypatch.setattr(GitHubClient, "create_pull_request", _fake_create_pull_request)
    monkeypatch.setattr(GitHubClient, "list_pull_requests_for_head", _fake_list_pull_requests_for_head)

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="open_pr")
    # A completed repo_edit sibling's own `completed` event is how _handle_open_pr() finds the
    # branch to open a PR for -- see _find_repo_edit_branch().
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=owner_id,
            event_type=MainAITaskEventType.completed,
            detail={"work_result": {"branch": "claude/mainai-task-abc", "base_branch": "claude/det-kommer-mer-879lcm"}},
        )
    )
    db_session.commit()

    first = await _handle_open_pr(db_session, task, goal)
    assert first["proposed"] is False
    assert first["pull_request_number"] == 42

    # Simulates the crash: no checkpoint was recorded for `first`'s result, so
    # run_task_execution_job() would call _handle_open_pr() again on resume.
    second = await _handle_open_pr(db_session, task, goal)
    assert second["proposed"] is False
    assert second["pull_request_number"] == 42

    assert create_calls["n"] == 1, "create_pull_request() must never be called twice for the same head/base"


def test_record_engineering_lesson_for_the_handle_open_pr_resume_fix(db_session, owner_id):
    """MAINAI V0.1 hardening pass (post-PR #57): engineering learning loop, mandatory for a P1
    finding. Crash matrix point I ('after durable checkpoint before PR step', read here as
    'the PR step itself IS the uncheckpointed work') found the same class of bug as point H,
    in a sibling code path."""
    lesson = lessons.record_lesson(
        db_session,
        problem=(
            "open_pr's real PR creation happens INSIDE the same 'work_result' computation "
            "run_task_execution_job() checkpoints -- unlike repo_edit, there is no separate "
            "'finalized' checkpoint for it. A crash between a successful create_pull_request() "
            "call and that checkpoint's commit meant resume called _handle_open_pr() again "
            "from scratch. create_pull_request() is not naturally idempotent -- GitHub rejects "
            "a second open PR for the same head/base with a 422 -- so the task would land on "
            "retryable_failed/failed even though the first PR had already durably succeeded."
        ),
        root_cause=(
            "_handle_open_pr() always called create_pull_request() unconditionally, with no "
            "check for whether a PR for this exact head/base already existed -- the same root "
            "cause as _finalize_repo_edit()'s own pre-fix bug (crash matrix point H), just "
            "in a sibling task_type handler that was not covered by that first fix."
        ),
        affected_component="app.mainai_execution.execution_job._handle_open_pr",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Reproduced with a stateful fake GitHubClient: calling _handle_open_pr() twice for "
            "the same task/goal (simulating dispatch, then a crash before the checkpoint, then "
            "a resume) with the pre-fix code called create_pull_request() twice."
        ),
        fix=(
            "Added GitHubClient.list_pull_requests_for_head() and wired it into "
            "_handle_open_pr(): checks for an existing open PR on the exact head/base BEFORE "
            "calling create_pull_request(), reusing it on resume instead of creating a "
            "duplicate."
        ),
        general_rule=(
            "A checkpoint-then-resume pattern only closes a crash window for the ONE call site "
            "it was built for -- a sibling code path with the same 'real external side effect "
            "computed as part of a checkpointed work_result' shape needs the SAME idempotency "
            "check applied independently; fixing one instance of a pattern does not fix the "
            "pattern everywhere it appears."
        ),
        applies_to=["open_pr", "github_client", "checkpoint", "crash_matrix"],
        source_type="branch_registry_pass",
        source_ref="MAINAI V0.1 hardening pass (post-PR #57) -- crash matrix point I, _handle_open_pr resume fix",
        created_by="hardening-pass",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_executor.py::test_handle_open_pr_resumes_correctly_without_creating_a_duplicate_pr",
    )
    db_session.commit()

    assert lesson.id is not None
    found = lessons.lookup_lessons(db_session, applies_to_any=["open_pr"])
    assert any(item.id == lesson.id for item in found)


def test_record_engineering_lesson_for_the_finalize_repo_edit_resume_fix(db_session, owner_id):
    """MAINAI V0.1 hardening pass (post-PR #57): engineering learning loop, mandatory for a P1
    finding. Crash matrix point H ('after GitHub commit before durable checkpoint') found a
    real truthfulness bug in _finalize_repo_edit()'s resume path."""
    lesson = lessons.record_lesson(
        db_session,
        problem=(
            "A crash between _finalize_repo_edit()'s real GitHub push succeeding and "
            "run_task_execution_job()'s 'finalized' checkpoint commit landing meant a resume "
            "re-ran _finalize_repo_edit() from scratch: it re-read base_sha from the shared "
            "base_branch (unchanged by the first push), saw the task's own branch already "
            "existed, and built the new commit on the STALE base_sha instead of the branch's "
            "real current tip. GitHub's fast-forward-only update_ref() correctly rejected the "
            "non-fast-forward update, but the resulting GitHubClientError landed the task on "
            "retryable_failed/failed -- even though the first attempt's push had already "
            "durably succeeded. A task could end up permanently `failed` in the final report "
            "while the real code change sat live on its branch the whole time."
        ),
        root_cause=(
            "_finalize_repo_edit() always recomputed base_sha from base_branch and treated "
            "'branch already exists' purely as a signal to skip create_branch(), never as a "
            "signal that base_sha itself might now be stale relative to that branch's own "
            "history."
        ),
        affected_component="app.mainai_execution.execution_job._finalize_repo_edit",
        severity=EngineeringLessonSeverity.high,
        evidence=(
            "Reproduced with a stateful fake GitHubClient modeling the real fast-forward-only "
            "update_ref() semantics: calling _finalize_repo_edit() twice for the same task "
            "(simulating dispatch, then a crash before the checkpoint, then a resume) with the "
            "pre-fix code raised GitHubClientError on the second call via a genuine "
            "non-fast-forward rejection."
        ),
        fix=(
            "_finalize_repo_edit() now tries get_ref(branch) FIRST -- if the task's own branch "
            "already exists (a resume), base_sha becomes its real current tip, and the new "
            "commit is built on top of that (a true fast-forward) instead of the original, "
            "stale base_branch sha. A genuinely first attempt (branch doesn't exist, a real "
            "404/GitHubClientError) still creates it from base_branch as before."
        ),
        general_rule=(
            "A resumable side effect against an external system with its own consistency "
            "rules (here: Git's fast-forward requirement) must re-derive its 'base' state from "
            "that system's OWN current state on resume, never from a value captured before the "
            "side effect it is checkpointing might have already happened -- 'the branch already "
            "exists' is a signal to re-read reality, not just a reason to skip one step."
        ),
        applies_to=["repo_edit", "github_client", "checkpoint", "crash_matrix"],
        source_type="branch_registry_pass",
        source_ref="MAINAI V0.1 hardening pass (post-PR #57) -- crash matrix point H, _finalize_repo_edit resume fix",
        created_by="hardening-pass",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_executor.py::test_finalize_repo_edit_resumes_correctly_when_the_branch_already_exists_from_a_prior_uncheckpointed_push",
    )
    db_session.commit()

    assert lesson.id is not None
    found = lessons.lookup_lessons(db_session, applies_to_any=["repo_edit"])
    assert any(item.id == lesson.id for item in found)


@pytest.mark.asyncio
async def test_run_task_execution_job_run_tests_rejects_a_path_traversing_target_even_if_persisted(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """Hardening pass finding (P1), third layer of defense: planner.create_plan() now rejects
    an unsafe `targeted_tests` target at plan time (see test_mainai_execution_planner.py), so
    this simulates a target that reached the database some other way (e.g. pre-hardening data,
    or a future insert path that skips create_plan()) -- execution_job.py's own `_run_pytest()`
    must still refuse it at the actual subprocess boundary, counted here via a real
    subprocess.run patch to prove the dangerous call never happens at all."""
    import subprocess

    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend").mkdir(parents=True)

    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="run_tests", verification_plan=[{"kind": "targeted_tests", "target": "tests/ok.py"}])
    # Bypasses planner.create_plan()'s own rejection to simulate an unsafe target that reached
    # the database some other way -- the point of this test.
    task.verification_plan = [{"kind": "targeted_tests", "target": "../../../etc/passwd"}]
    db_session.flush()

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    assert call_count["n"] == 0, "subprocess.run must never be invoked with the unsafe target"

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.status != MainAITaskStatus.completed


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
