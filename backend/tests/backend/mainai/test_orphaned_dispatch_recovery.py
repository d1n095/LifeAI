"""T2 -- a dispatch committed but never claimed is recovered, not lost (assessment + pin).

Measured 2026-11-27 in the isolated stack: `dispatch_ready_task()` commits the task's
`running` status and the durable `task_execution` job in one atomic commit; the job is claimed
by `prepare_context()` in a SEPARATE statement. A worker killed in exactly that gap leaves the
job `queued` and the task `running`, and the E2E run reported it as unrecoverable, with
`attempts` frozen and the attempts budget never triggering.

Re-measured before changing anything (2026-11-27, this file): that conclusion does NOT hold.
A leftover `queued` job is claimed by the ordinary `claim_next_mainai_job()` poll, and then:

  * never-governed goal  -> the real V0.1 executor runs the task normally;
  * envelope-governed    -> F1's structural fence refuses V0.1 and finalizes the task through
                            the ONE existing outcome gate: `retryable_failed` with a real
                            `next_retry_at` (or `failed` once the budget is exhausted), and
                            the job becomes `failed`/`capability_unavailable`.

That holds even in the narrower crash window where `mainai_job_id` was never written (it is a
real FK, so it can only be set after `create_job()`'s commit): the fence matches
`task.mainai_job_id IN (None, job.id)` and the retry tick then moves the task back to `ready`
for its own dispatcher. So T2 is NOT a blocking defect and no new recovery path is added here;
this file pins the real behavior instead, including the authority boundary (a governed goal is
never redispatched by the envelope-blind V0.1 tick).
"""

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import latest_checkpoint_for_step
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.models.mainai_job import MainAIJob
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


def _dispatch_without_claim(db_session, owner_id, *, governed: bool):
    """The exact crash state: dispatch committed, the job never claimed by anyone."""
    goal = planner.create_goal(
        db_session,
        owner_id=owner_id,
        title="Orphaned dispatch",
        original_instruction="Do it.",
        created_by="test",
    )
    if governed:
        from app.execution_envelopes import authorize_execution_scope, propose_execution_scope

        _set_rls_user(db_session, owner_id)
        proposal = propose_execution_scope(
            db_session,
            owner_id=owner_id,
            goal_id=goal.id,
            idempotency_key=f"orphan-proposal-{goal.id}",
            proposed_paths=["README.md"],
            proposed_capabilities=["read_file"],
            proposed_risk="low",
        )
        authorize_execution_scope(
            db_session,
            owner_id=owner_id,
            proposal_id=proposal.id,
            authorized_by="founder",
            authorized_paths=["README.md"],
            authorized_capabilities=["read_file"],
            authorized_risk="low",
            envelope_idempotency_key=f"orphan-envelope-{goal.id}",
        )
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)
    return goal, task, job


async def _claim_and_run(db_session, superuser_db, owner_id, job: MainAIJob) -> None:
    claimed = claim_next_mainai_job(superuser_db, "restarted-worker", 120)
    assert claimed is not None and claimed[0] == job.id
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(
        db_session,
        job.id,
        owner_id,
        worker_id="restarted-worker",
        lease_generation=claimed[2],
        lease_seconds=120,
    )


def _task_state(superuser_db, task_id):
    return superuser_db.execute(
        sa_text("SELECT status, attempts, next_retry_at, mainai_job_id FROM mainai_tasks WHERE id = :id"),
        {"id": str(task_id)},
    ).one()


def _job_state(superuser_db, job_id):
    return superuser_db.execute(
        sa_text("SELECT status, error_category FROM mainai_jobs WHERE id = :id"),
        {"id": str(job_id)},
    ).one()


def _jobs_for(superuser_db, task_id):
    return superuser_db.execute(
        sa_text("SELECT id, status FROM mainai_jobs WHERE input_refs::text LIKE :ref ORDER BY created_at"),
        {"ref": f"%{task_id}%"},
    ).all()


@pytest.mark.asyncio
async def test_a_governed_leftover_dispatch_is_fenced_and_restored_to_ready(
    db_session, superuser_db, owner_id
):
    _goal, task, job = _dispatch_without_claim(db_session, owner_id, governed=True)
    assert _job_state(superuser_db, job.id).status == "queued"

    # The ordinary poll claims it after the restart; F1's fence must refuse V0.1.
    await _claim_and_run(db_session, superuser_db, owner_id, job)

    assert _job_state(superuser_db, job.id).status == "failed"
    assert _job_state(superuser_db, job.id).error_category == "capability_unavailable"
    state = _task_state(superuser_db, task.id)
    assert state.status == "retryable_failed", "the task must be finalized, never left running"
    assert state.next_retry_at is not None, "a real backoff clock must exist"
    assert state.attempts == 1
    assert latest_checkpoint_for_step(superuser_db, task_id=task.id, job_id=job.id, step="work_result") is None

    # The existing retry tick restores it; the goal's own Supervisor tick owns redispatch.
    superuser_db.execute(
        sa_text("UPDATE mainai_tasks SET next_retry_at = now() - interval '1 second' WHERE id = :id"),
        {"id": str(task.id)},
    )
    superuser_db.commit()
    Worker()._advance_mainai_execution_retries(superuser_db)
    assert _task_state(superuser_db, task.id).status == "ready"

    Worker()._advance_mainai_execution_tasks(superuser_db)
    assert len(_jobs_for(superuser_db, task.id)) == 1, "a governed goal must never be redispatched by V0.1"
    assert _task_state(superuser_db, task.id).status == "ready"


@pytest.mark.asyncio
async def test_the_same_recovery_works_when_mainai_job_id_was_never_written(
    db_session, superuser_db, owner_id
):
    """The narrower crash window: `create_job()` committed, the FK column never written."""
    _goal, task, job = _dispatch_without_claim(db_session, owner_id, governed=True)
    superuser_db.execute(
        sa_text("UPDATE mainai_tasks SET mainai_job_id = NULL WHERE id = :id"), {"id": str(task.id)}
    )
    superuser_db.commit()

    await _claim_and_run(db_session, superuser_db, owner_id, job)

    state = _task_state(superuser_db, task.id)
    assert _job_state(superuser_db, job.id).status == "failed"
    assert state.status == "retryable_failed"
    assert state.next_retry_at is not None


@pytest.mark.asyncio
async def test_a_never_governed_leftover_dispatch_is_simply_executed(
    db_session, superuser_db, owner_id, monkeypatch
):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content="Analysen visar inga problem.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)

    _goal, task, job = _dispatch_without_claim(db_session, owner_id, governed=False)
    await _claim_and_run(db_session, superuser_db, owner_id, job)

    assert _job_state(superuser_db, job.id).status == "completed"
    assert _task_state(superuser_db, task.id).status == "completed"
