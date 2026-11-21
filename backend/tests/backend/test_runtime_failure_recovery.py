"""M4 — the failure shapes that decide whether an interrupted attempt is recoverable or a lie.

Three properties, each driven through the real job path against real PostgreSQL:

  1. **A lost database connection must not fabricate an outcome.** When the worker's connection dies
     mid-attempt, whatever is durable is what a restart will act on. If the job row were already
     `failed`/`completed` while the task never reached its own terminal state, no mechanism would
     ever revisit that task (a terminal `mainai_jobs` status is not reclaimable) — a permanently
     stuck task. The correct durable shape after a crash is "job still running, lease expiring",
     which the reclaim/recovery path is built to handle.

  2. **A worker whose lease was reclaimed must never reach the one not-safely-repeatable external
     side effect.** `_finalize_repo_edit()` pushes a real branch. The fence immediately before it is
     what stops a stale worker from pushing a second commit on a branch another claimant already
     owns.

  3. **A reclaim genuinely transfers execution**: the old claimant's writes are refused by the
     generation fence, and the new claimant's are accepted.
"""

import json
import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError

from app.db_clock import db_now
from app.jobs.mainai_job_lease import JobLeaseLostError, claim_next_mainai_job, renew_mainai_job_lease
from app.mainai_execution import executor, planner
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.liveness import TaskLiveness, task_liveness
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.verify import VerificationResult
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
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


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _dispatch(db_session, owner_id, *, task_type: str = "repo_edit"):
    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Runtime failure recovery",
        original_instruction="Do the thing.",
        created_by="test",
        approval_policy="standard_repo_work",
    )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[PlannedTaskSpec(description="the task", task_type=task_type)],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    return task, job


@pytest.mark.asyncio
async def test_a_lost_database_connection_leaves_a_recoverable_attempt_not_a_false_outcome(
    db_session, superuser_db, owner_id, monkeypatch
):
    """The database goes away at the first durable-write boundary of an attempt — the shape of an
    OOM-killed container or a failover, not an exception the code designed for.

    The fault is injected at `record_checkpoint` rather than by terminating the session's backend.
    Terminating it is order-dependent in a full suite: SQLAlchemy reconnects transparently, so
    whether the failure lands at all depends on which pooled connection the next statement draws —
    and a flaky test of a crash property is worse than no test, because a green run then proves
    nothing.
    """
    from sqlalchemy.exc import OperationalError

    from app.mainai_execution import execution_job

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("read-only audit complete"))

    def _connection_lost(*_args, **_kwargs):
        raise OperationalError("INSERT INTO mainai_checkpoints", {}, Exception("server closed the connection unexpectedly"))

    monkeypatch.setattr(execution_job, "record_checkpoint", _connection_lost)

    task, job = _dispatch(db_session, owner_id, task_type="read_only_audit")
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-crash", 120)
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)

    with pytest.raises(DBAPIError):
        await run_task_execution_job(
            db_session, job.id, owner_id, worker_id="worker-crash", lease_generation=generation, lease_seconds=120
        )
    # A process that loses its connection does not keep its transaction: the server aborts it. The
    # test has to do the same, or the attempt's row locks stay held and the next statement blocks
    # forever -- the exact failure this file's suite-level fix exists to make visible.
    db_session.rollback()

    # The durable, restart-visible truth: nothing claims an outcome it never reached.
    superuser_db.commit()
    superuser_db.expire_all()
    job_row = superuser_db.get(MainAIJob, job.id)
    task_row = superuser_db.get(MainAITask, task.id)
    assert job_row.status == MainAIJobStatus.running, "a crashed attempt must stay reclaimable, never terminal"
    assert task_row.status == MainAITaskStatus.running

    # And once its lease expires it is exactly the dead-attempt shape recovery acts on — not a
    # task that silently looks finished.
    superuser_db.execute(
        sa_text(
            "UPDATE mainai_jobs SET lease_expires_at = (clock_timestamp() AT TIME ZONE 'UTC') - interval '1 minute' "
            "WHERE id = :id"
        ),
        {"id": str(job.id)},
    )
    superuser_db.commit()
    superuser_db.expire_all()
    assert (
        task_liveness(
            superuser_db.get(MainAITask, task.id),
            superuser_db.get(MainAIJob, job.id),
            now=db_now(superuser_db),
        )
        is TaskLiveness.stalled
    )


@pytest.mark.asyncio
async def test_a_worker_that_lost_its_lease_never_reaches_the_push(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """The last fence before the irreversible step. `_finalize_repo_edit()` performs a real
    `git push`; a worker whose lease was reclaimed must stop before it, because a second push on a
    branch another claimant now owns is precisely the duplicate privileged execution a lease
    exists to prevent."""
    from app.mainai_execution import execution_job

    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(execution_job, "_repo_root", lambda: shared)
    monkeypatch.setattr(
        OpenAIProvider,
        "chat",
        _fake_chat(json.dumps({"files": [{"path": "x.md", "content": "y"}], "commit_message": "m"})),
    )
    monkeypatch.setattr(execution_job, "verify_task", lambda *a, **k: VerificationResult(passed=True, steps=[]))
    pushed: list[str] = []

    async def _record_push(db, task, work_result):
        pushed.append(str(task.id))
        return {"branch": "b", "proposed": True, "files": ["x.md"]}

    monkeypatch.setattr(execution_job, "_finalize_repo_edit", _record_push)

    real_renew = renew_mainai_job_lease
    calls = {"n": 0}

    def _renew_then_lose_the_lease(db, job_id, worker_id, lease_generation, lease_seconds):
        calls["n"] += 1
        if calls["n"] >= 4:  # dispatch, work checkpoint, verification checkpoint, then the push fence
            raise JobLeaseLostError(job_id, worker_id, lease_generation)
        return real_renew(db, job_id, worker_id, lease_generation, lease_seconds)

    monkeypatch.setattr(execution_job, "renew_mainai_job_lease", _renew_then_lose_the_lease)

    task, job = _dispatch(db_session, owner_id)
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-stale", 120)
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)

    await run_task_execution_job(
        db_session, job.id, owner_id, worker_id="worker-stale", lease_generation=generation, lease_seconds=120
    )

    assert pushed == [], "a worker that lost its lease must never push"
    superuser_db.commit()
    superuser_db.expire_all()
    assert superuser_db.get(MainAITask, task.id).status != MainAITaskStatus.completed


def test_an_expired_task_execution_lease_is_never_handed_to_a_second_worker(
    db_session, superuser_db, owner_id
):
    """The headline invariant: `task_execution` is the one job type with real external side
    effects, so its expired lease must NOT be silently reclaimed by the next polling worker.
    It stays `running`, owned by the dead attempt, until the recovery pipeline processes it
    through inspect -> classify -> takeover. This is the difference between "an interrupted
    attempt" and "two workers executing the same privileged work"."""
    _dispatch(db_session, owner_id)
    first_job, first_owner, _first_generation = claim_next_mainai_job(superuser_db, "worker-a", 120)
    superuser_db.commit()

    superuser_db.execute(
        sa_text(
            "UPDATE mainai_jobs SET lease_expires_at = (clock_timestamp() AT TIME ZONE 'UTC') - interval '1 second' "
            "WHERE id = :id"
        ),
        {"id": str(first_job)},
    )
    superuser_db.commit()

    assert claim_next_mainai_job(superuser_db, "worker-b", 120) is None, (
        "an expired task_execution lease must never be blind-reclaimed -- that is duplicate execution"
    )
    superuser_db.rollback()
    superuser_db.expire_all()

    row = superuser_db.get(MainAIJob, first_job)
    assert row.status == MainAIJobStatus.running
    assert row.locked_by == "worker-a", "the dead attempt's ownership must not silently move"
