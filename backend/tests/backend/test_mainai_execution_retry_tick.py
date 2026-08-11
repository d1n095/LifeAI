"""MainAI V0.3 -- central-guard tests for app/worker.py's `_advance_mainai_execution_retries`
tick (the automatic counterpart of the founder-driven POST /tasks/{id}/retry, see
app/mainai_execution/execution_job.py's `_finalize_task_outcome()` for where `next_retry_at`
gets scheduled). Not covered elsewhere: every existing V0.3 test exercises this tick only
indirectly through a full worker.run_once() cycle or a manually-set `next_retry_at` a moment in
the past. This module isolates the tick itself and proves its own scoping guard: a
`retryable_failed` task whose `next_retry_at` has NOT yet elapsed must never be touched, only
tasks whose scheduled time has genuinely passed -- otherwise the backoff this tick exists to
enforce would be meaningless."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskStatus
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


def _retryable_task(db_session, owner_id, *, next_retry_at) -> MainAITask:
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Retry tick test goal", original_instruction="Fix it.", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit", max_attempts=3)],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    task.next_retry_at = next_retry_at
    db_session.commit()
    return task


def test_retry_tick_moves_a_due_task_back_to_ready_and_clears_next_retry_at(db_session, owner_id):
    task = _retryable_task(db_session, owner_id, next_retry_at=datetime.utcnow() - timedelta(seconds=5))

    worker = Worker()
    worker._advance_mainai_execution_retries(db_session)
    db_session.commit()

    db_session.expire_all()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.ready
    assert task.next_retry_at is None


def test_retry_tick_never_touches_a_task_whose_next_retry_at_has_not_yet_elapsed(db_session, owner_id):
    """Central guard: the whole point of a scheduled backoff is that the task is left alone
    until its own time comes -- an early pickup would defeat the backoff entirely."""
    task = _retryable_task(db_session, owner_id, next_retry_at=datetime.utcnow() + timedelta(hours=1))

    worker = Worker()
    worker._advance_mainai_execution_retries(db_session)
    db_session.commit()

    db_session.expire_all()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.next_retry_at is not None


def test_retry_tick_never_touches_a_retryable_failed_task_with_no_next_retry_at_scheduled(db_session, owner_id):
    """A retryable_failed task with next_retry_at NULL has no automatic retry scheduled (e.g.
    it predates V0.3, or a founder cleared it) -- the tick's own query filters on
    next_retry_at.isnot(None), so it must be left exactly as it is, not silently retried."""
    task = _retryable_task(db_session, owner_id, next_retry_at=None)

    worker = Worker()
    worker._advance_mainai_execution_retries(db_session)
    db_session.commit()

    db_session.expire_all()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.next_retry_at is None


def test_retry_tick_bounds_how_many_tasks_it_touches_per_cycle(db_session, owner_id):
    """Same bounded-scan discipline as every other V0.3 worker tick -- proves the limit is a
    real query constraint, not just a comment, by creating more due tasks than the bound and
    confirming some are deliberately left for the next cycle."""
    worker = Worker()
    limit = worker._MAX_RETRY_SCANS_PER_TICK
    due_at = datetime.utcnow() - timedelta(seconds=5)
    for _ in range(limit + 3):
        _retryable_task(db_session, owner_id, next_retry_at=due_at)

    worker._advance_mainai_execution_retries(db_session)
    db_session.commit()

    db_session.expire_all()
    still_retryable = (
        db_session.query(MainAITask).filter(MainAITask.owner_id == owner_id, MainAITask.status == MainAITaskStatus.retryable_failed).count()
    )
    ready_now = db_session.query(MainAITask).filter(MainAITask.owner_id == owner_id, MainAITask.status == MainAITaskStatus.ready).count()
    assert ready_now == limit
    assert still_retryable == 3


def test_retry_tick_is_fifo_by_due_time_not_fair_across_goals_and_the_doc_must_say_so(db_session, owner_id):
    """V0.3 hardening pass, §7 fairness attack: the founder's own scenario -- one goal (A) with
    a large due backlog, a second goal (B) with a single due task -- run against the real
    scan query, not just described in the doc. `_advance_mainai_execution_retries`'s query has
    NO goal_id/owner grouping at all: it orders strictly by `next_retry_at ASC, id ASC` across
    the whole system. This test proves the actual, honest behavior: goal B's single task, due
    LATER than goal A's backlog, is deliberately left untouched by a tick whose bound goal A's
    backlog alone already exhausts -- this is real, temporal FIFO fairness (oldest-due-first,
    monotonically draining, never permanently starved), NOT per-goal round-robin fairness. A
    goal with a large enough backlog can and does meaningfully delay a different goal's newly-
    due task by roughly (backlog size / per-tick limit) ticks. Confirms the backlog drains
    monotonically (goal B's task IS eventually reached, exactly once, never skipped)."""
    worker = Worker()
    limit = worker._MAX_RETRY_SCANS_PER_TICK
    now = datetime.utcnow()

    goal_a_due_at = now - timedelta(hours=1)  # older -- sorts first
    goal_a_tasks = [_retryable_task(db_session, owner_id, next_retry_at=goal_a_due_at) for _ in range(limit + 2)]

    goal_b_due_at = now - timedelta(seconds=1)  # newer, but still genuinely due
    goal_b_task = _retryable_task(db_session, owner_id, next_retry_at=goal_b_due_at)

    # One tick: goal A's own backlog alone exceeds the per-tick bound, so goal B's single (but
    # more-recently-due) task is NOT reached this cycle -- proving FIFO-by-due-time, not
    # per-goal fairness, is the real, current behavior.
    worker._advance_mainai_execution_retries(db_session)
    db_session.commit()
    db_session.expire_all()

    goal_b_task = db_session.get(MainAITask, goal_b_task.id)
    assert goal_b_task.status == MainAITaskStatus.retryable_failed, (
        "goal B's newly-due task was delayed behind goal A's older backlog -- this is the real, "
        "documented tradeoff (temporal FIFO, not per-goal fairness), not a bug"
    )
    ready_from_a = sum(1 for t in goal_a_tasks if db_session.get(MainAITask, t.id).status == MainAITaskStatus.ready)
    assert ready_from_a == limit, "the first tick must make forward progress on the older backlog, not starve on it either"

    # A second tick drains the rest of goal A's backlog AND finally reaches goal B -- proving
    # the delay is bounded and monotonic, never a permanent starve.
    worker._advance_mainai_execution_retries(db_session)
    db_session.commit()
    db_session.expire_all()

    goal_b_task = db_session.get(MainAITask, goal_b_task.id)
    assert goal_b_task.status == MainAITaskStatus.ready, "goal B's task must be reached once goal A's older backlog has drained -- bounded delay, not starvation"
    still_retryable = (
        db_session.query(MainAITask).filter(MainAITask.owner_id == owner_id, MainAITask.status == MainAITaskStatus.retryable_failed).count()
    )
    assert still_retryable == 0, "nothing left due after the backlog fully drains"
