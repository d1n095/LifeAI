"""V0.2 hardening finding: run_task_execution_job()'s two `record_checkpoint()` calls
(work_result, finalized) used to commit with NO lease re-verification, even though task-state/
verification writes were already correctly fenced (via the atomic commit they share with
mark_completed()/mark_failed()). A worker whose lease had already expired and been reclaimed
by someone else (`claim_next_mainai_job()`'s own real reclaim -- same job_id, bumped
lease_generation, exactly as it already does for every mainai_jobs row) could still land a
checkpoint write for a job it no longer owns. This module proves the fix: a checkpoint write
attempted under a STALE (worker_id, lease_generation) is rejected before anything durable
lands, while the CURRENT legitimate claimant's own checkpoint write still succeeds normally."""

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import latest_checkpoint_for_step
from app.mainai_execution.execution_job import run_task_execution_job
from app.models.mainai_execution import MainAITask
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


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


@pytest.mark.asyncio
async def test_stale_worker_cannot_land_a_work_result_checkpoint_after_being_reclaimed(db_session, superuser_db, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Analysen visar inga problem."))

    goal = planner.create_goal(db_session, owner_id=owner_id, title="Fencing test goal", original_instruction="Audit something.", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    # worker-1 claims it first (generation 1).
    job_id, _owner, stale_generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert stale_generation == 1

    # Simulate the exact staleness scenario this fix protects against. Since migration 0034,
    # `claim_next_mainai_job()` no longer blind-reclaims an expired-lease `task_execution` job
    # itself (see that function's own docstring/SQL) -- a dead one only ever gets a fresh
    # replacement job through app/mainai_execution/recovery_takeover.py, never a same-job_id
    # reclaim. This UPDATE stands in for "some mechanism legitimately took over this exact
    # row" (a manual operator override, or a future change to that policy) so this test still
    # proves the underlying invariant: whoever holds the CURRENT lease_generation is the only
    # one whose writes may land, regardless of how that generation came to be current.
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET locked_by = 'worker-2', lease_generation = lease_generation + 1 WHERE id = :id"),
        {"id": str(job_id)},
    )
    superuser_db.commit()
    new_generation = stale_generation + 1

    _set_rls_user(db_session, owner_id)

    # worker-1 (stale, still believes it holds generation 1) now tries to actually run the job.
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=stale_generation, lease_seconds=120)

    # Nothing durable was written by the stale attempt: no work_result checkpoint exists yet.
    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result") is None
    db_session.refresh(task)
    from app.models.mainai_execution import MainAITaskStatus

    assert task.status == MainAITaskStatus.running  # untouched -- the stale attempt made zero progress

    # worker-2 (the real current claimant) can still successfully run and checkpoint the SAME job.
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-2", lease_generation=new_generation, lease_seconds=120)
    checkpoint = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result")
    assert checkpoint is not None
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


def test_claim_next_mainai_job_never_blind_reclaims_a_dead_task_execution_job(db_session, superuser_db, owner_id):
    """The actual SQL invariant migration 0034 establishes: once a `task_execution` job's
    lease expires, `claim_next_mainai_job()` must never hand it back out on its own --
    only app/mainai_execution/recovery_takeover.py's explicit inspect->classify->salvage gate
    may ever act on it. A `queued` task_execution job must still be claimable normally (the
    exclusion is scoped to the expired-lease reclaim branch only, never fresh dispatch)."""
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Reclaim exclusion test goal", original_instruction="x", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    job_id, _owner, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert job_id == job.id
    assert generation == 1

    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(job_id)})
    superuser_db.commit()

    # Nothing else is claimable in this test's isolated owner scope -- a dead task_execution
    # job with an expired lease must be structurally invisible to this query, not merely
    # deprioritized.
    assert claim_next_mainai_job(superuser_db, "worker-2", 120) is None

    row = superuser_db.execute(sa_text("SELECT status, locked_by, lease_generation FROM mainai_jobs WHERE id = :id"), {"id": str(job_id)}).one()
    assert row[0] == "running"
    assert row[1] == "worker-1"
    assert row[2] == 1  # completely untouched by the failed reclaim attempt
