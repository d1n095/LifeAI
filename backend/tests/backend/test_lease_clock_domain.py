"""M4 — lease expiry is a database fact, so it is judged by the database's clock.

`mainai_jobs.lease_expires_at` is authored only by Postgres (`now() + make_interval(...)` in
`app/jobs/mainai_job_lease.py`). `_advance_mainai_execution_auto_recovery()` compared it against
Python's `datetime.utcnow()` — two clocks, one comparison. This file injects a realistic clock
skew (the shape a VPS container gets after an NTP step or a snapshot boot) and asserts both
directions behave correctly:

  - **app clock ahead** — old behaviour: a live lease reads as expired, the tick takes over a
    genuinely running attempt, and the same privileged work is executed twice. Now: untouched.
  - **app clock behind** — old behaviour: an expired lease reads as live, the crashed attempt is
    never recovered and the task stays `running` forever. Now: detected and recovered.

The skew is injected by replacing the `datetime` symbol inside `app.worker`, so it affects
exactly what the tick reads and nothing else — a fault injection, not a mock of the tick.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.db_clock import db_now
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.liveness import TaskLiveness, task_liveness
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAIRecoveryRecord
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


def _skew_worker_clock(monkeypatch, delta: timedelta) -> None:
    """Make everything inside app.worker read a clock that is `delta` off the real one."""
    import app.worker as worker_module

    real = datetime

    class _SkewedDatetime(real):  # subclass so isinstance/typing elsewhere still works
        @classmethod
        def utcnow(cls):
            return real.utcnow() + delta

    monkeypatch.setattr(worker_module, "datetime", _SkewedDatetime)


def _dispatch_and_claim(db_session, superuser_db, owner_id):
    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Clock-domain probe",
        original_instruction="Read-only audit.",
        created_by="test",
        approval_policy="standard_repo_work",
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    claimed, _worker, generation = claim_next_mainai_job(superuser_db, "worker-skew", 120)
    assert claimed is not None and generation is not None
    # The claim session must not carry an open transaction into the next test: an uncommitted
    # transaction keeps row locks on mainai_jobs, and a later teardown that truncates the table
    # then blocks on them -- which is exactly how a suite-level hang looks from the outside.
    superuser_db.commit()
    db_session.expire_all()
    return task, job


@pytest.mark.asyncio
async def test_a_live_lease_is_never_declared_dead_when_the_app_clock_runs_ahead(
    db_session, superuser_db, owner_id, monkeypatch
):
    """The duplicate-execution direction. A 3-hour-forward clock is far larger than any NTP
    step, which is the point: skew must not be able to turn a live attempt into a takeover."""
    task, job = _dispatch_and_claim(db_session, superuser_db, owner_id)
    job_id = job.id

    _skew_worker_clock(monkeypatch, timedelta(hours=3))

    await Worker()._advance_mainai_execution_auto_recovery(superuser_db)

    record = superuser_db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == job_id).one_or_none()
    assert record is None, "a live lease must never be recovered merely because the app clock runs ahead"
    status = superuser_db.execute(
        sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(job_id)}
    ).scalar_one()
    assert status == "running"
    task_row = superuser_db.get(MainAITask, task.id)
    assert task_row.status == MainAITaskStatus.running  # untouched, still executing


@pytest.mark.asyncio
async def test_an_expired_lease_is_still_detected_when_the_app_clock_runs_behind(
    db_session, superuser_db, owner_id, monkeypatch
):
    """The never-recovered direction: the fake clock says the lease has hours left, the real
    database says it expired five minutes ago. The database must win."""

    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content="Audit complete, no findings.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)

    task, job = _dispatch_and_claim(db_session, superuser_db, owner_id)
    job_id = job.id
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '5 minutes' WHERE id = :id"),
        {"id": str(job_id)},
    )
    superuser_db.commit()

    _skew_worker_clock(monkeypatch, timedelta(hours=-3))

    await Worker()._advance_mainai_execution_auto_recovery(superuser_db)

    record = superuser_db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == job_id).one_or_none()
    assert record is not None, "an expired lease must be recovered even when the app clock runs behind"
    superuser_db.commit()


def test_liveness_verdicts_follow_the_database_clock(db_session, superuser_db, owner_id):
    """`task_liveness()` is pure, so the caller supplies the clock. Judged with Python's wall
    clock a live job reads `stalled`; judged with `db_now()` it reads `running`, which is what
    every API surface now passes.

    Deliberately NO claim here: this is a pure-function question about a live lease, and taking
    a real claim would only add lock lifetime this test has no use for."""
    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Liveness clock probe",
        original_instruction="Read-only audit.",
        created_by="test",
        approval_policy="standard_repo_work",
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    job = superuser_db.get(MainAIJob, task.mainai_job_id)
    # Give the job a real future lease, DB-authored, exactly like claim_next_mainai_job() does.
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET status = 'running', lease_expires_at = now() + interval '120 seconds' WHERE id = :id"),
        {"id": str(job.id)},
    )
    superuser_db.commit()
    superuser_db.expire_all()
    job = superuser_db.get(MainAIJob, job.id)

    assert task_liveness(task, job, now=datetime.utcnow() + timedelta(hours=3)) is TaskLiveness.stalled
    assert task_liveness(task, job, now=db_now(superuser_db)) is TaskLiveness.running
