"""F1 — an envelope-governed goal is NEVER executed by the envelope-blind V0.1 executor,
and a permanently refused Supervisor tick PARKS its task instead of generating a new job every
poll cycle.

Measured in the isolated installation on 2026-11-27: a Supervisor tick called
`dispatch_ready_task()` (a real, durable `mainai_jobs` commit) and then failed planning with
`CandidateValidationError` (the goal's `approval_policy` was not scoped for unattended work).
The task stayed `running`, and the leftover `task_execution` job was picked up by
`claim_next_mainai_job()` -> `run_task_execution_job()` -> `_propose_repo_edit()`, writing in
the SHARED checkout with zero awareness of the goal's envelope. `_advance_mainai_execution_tasks`
already excluded governed goals, but the job-claim path did not.

The fix has two halves, both reusing existing state models:

  1. `run_task_execution_job()`'s own entry fence (`_fence_governed_task_execution`): an
     EVER_GOVERNED goal's job is fence-failed and, if the task is still `running` on it, the
     attempt is finalized through the ONE existing outcome gate — `retryable_failed` with the
     normal backoff clock, or `failed` once the existing attempts budget is exhausted.
  2. `run_authorized_goal_supervisor_tick()`'s `CandidateValidationError` park: the task moves
     to `blocked` with a durable reason and NO retry clock, and the goal lease is released.
     A founder re-authorizing the envelope wakes it exactly once
     (`wake_tasks_blocked_for_supervisor_refusal`).

Nothing here builds a second work engine: the park, the retry clock, the outcome gate, the
lease fence and the job terminal states are all the pre-existing ones.
"""

import threading
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.execution_job import (
    GOVERNED_EXECUTION_FENCE_REASON,
    _fence_governed_task_execution,
    run_task_execution_job,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.provider_wait_wake import (
    SUPERVISOR_REFUSAL_PARK_BLOCKER,
    SUPERVISOR_REFUSAL_PARK_REASON,
    is_supervisor_refusal_park,
    wake_tasks_blocked_for_supervisor_refusal,
)
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobEvent, MainAIJobEventType
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))


def _goal_with_ready_task(db, owner_id, *, task_type="repo_edit", approval_policy="standard_repo_work"):
    _set_rls_user(db, owner_id)
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title="governed execution fence test",
        original_instruction="edit a file",
        approval_policy=approval_policy,
        created_by="test",
    )
    planner.create_plan(
        db,
        goal=goal,
        rationale="single task",
        tasks=[PlannedTaskSpec(description="edit a file", task_type=task_type)],
        created_by="test",
    )
    db.flush()
    return goal


def _authorize(db, owner_id, goal_id):
    _set_rls_user(db, owner_id)
    proposal = propose_execution_scope(
        db, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"gov-prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"gov-env-{uuid.uuid4()}",
    )
    return envelope


def _dispatch_and_claim(db, superuser_db, owner_id, goal):
    """The exact leftover shape: dispatch commits the task -> running + a queued job."""
    task = db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    job = executor.dispatch_ready_task(db, task=task, goal=goal, dispatched_by="test-supervisor")
    db.commit()
    claimed = claim_next_mainai_job(superuser_db, "worker-v01", 120)
    assert claimed is not None
    job_id, claimed_owner_id, generation = claimed
    assert job_id == job.id and claimed_owner_id == owner_id
    return task, job, generation


def _task_event_count(db, task_id, event_type) -> int:
    return len(
        db.execute(
            select(MainAITaskEvent).where(
                MainAITaskEvent.task_id == task_id, MainAITaskEvent.event_type == event_type
            )
        )
        .scalars()
        .all()
    )


# ------------------------------------------------------------------ the fence itself


@pytest.mark.asyncio
async def test_a_leftover_governed_job_is_never_executed_by_v01(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task, job, generation = _dispatch_and_claim(db_session, superuser_db, owner.id, goal)

    _set_rls_user(db_session, owner.id)
    await run_task_execution_job(
        db_session, job.id, owner.id, worker_id="worker-v01", lease_generation=generation, lease_seconds=120
    )
    db_session.expire_all()

    # The job is terminal and refused -- never executed.
    job = db_session.get(MainAIJob, job.id)
    assert job.status == "failed"
    assert job.error_category == MainAIJobErrorCategory.capability_unavailable.value

    # Nothing from the V0.1 executor ever ran: no work_result checkpoint, no verification
    # start. (The fence is checked before the lease renewal / progress write.)
    checkpoints = (
        db_session.execute(select(MainAICheckpoint).where(MainAICheckpoint.task_id == task.id))
        .scalars()
        .all()
    )
    assert [c.executor_state.get("step") for c in checkpoints if c.executor_state.get("step") == "work_result"] == []
    assert _task_event_count(db_session, task.id, MainAITaskEventType.verification_started) == 0

    # The attempt is finalized through the existing outcome gate: retryable within budget,
    # with the existing backoff clock -- never `completed`, never stuck `running`.
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.retryable_failed
    assert task.next_retry_at is not None
    assert task.attempts == 1
    failure_event = (
        db_session.execute(
            select(MainAITaskEvent)
            .where(
                MainAITaskEvent.task_id == task.id,
                MainAITaskEvent.event_type == MainAITaskEventType.verification_failed,
            )
            .order_by(MainAITaskEvent.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert failure_event is not None
    assert failure_event.detail["error"] == GOVERNED_EXECUTION_FENCE_REASON


@pytest.mark.asyncio
async def test_a_superseded_envelope_still_fences_v01(db_session, superuser_db, make_verified_user):
    """REVOCATION != FALLBACK: any envelope row ever existing is the durable fact."""
    from app.models.execution_envelope import ExecutionAuthorizationEnvelope

    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task, job, generation = _dispatch_and_claim(db_session, superuser_db, owner.id, goal)

    db_session.query(ExecutionAuthorizationEnvelope).filter(
        ExecutionAuthorizationEnvelope.goal_id == goal.id
    ).update({"status": "superseded"})
    db_session.commit()

    _set_rls_user(db_session, owner.id)
    await run_task_execution_job(
        db_session, job.id, owner.id, worker_id="worker-v01", lease_generation=generation, lease_seconds=120
    )
    db_session.expire_all()

    assert db_session.get(MainAIJob, job.id).status == "failed"
    assert db_session.get(MainAITask, task.id).status == MainAITaskStatus.retryable_failed


def test_a_never_governed_goal_is_not_fenced(db_session, superuser_db, make_verified_user):
    """Control: V0.1's existing behavior for ordinary goals is byte-for-byte unchanged."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    db_session.commit()

    task, job, generation = _dispatch_and_claim(db_session, superuser_db, owner.id, goal)
    _set_rls_user(db_session, owner.id)

    assert (
        _fence_governed_task_execution(
            db_session, job=job, task=task, goal=goal, worker_id="worker-v01", lease_generation=generation
        )
        is False
    )
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(MainAIJob, job.id).status == "running"
    assert db_session.get(MainAITask, task.id).status == MainAITaskStatus.running


@pytest.mark.asyncio
async def test_a_parked_task_is_never_overwritten_by_the_v01_fence(db_session, superuser_db, make_verified_user):
    """The Supervisor's park is more authoritative than a leftover job: the fence neutralizes
    the job and leaves the parked task exactly as it was."""
    from app.development_supervisor.production_entry import park_tasks_after_supervisor_refusal

    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    envelope = _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task, job, generation = _dispatch_and_claim(db_session, superuser_db, owner.id, goal)

    # A config refusal parks the task before the leftover job is ever claimed by V0.1.
    parked = park_tasks_after_supervisor_refusal(
        db_session,
        goal=goal,
        attempted=[(task.id, job.id)],
        execution_envelope_id=envelope.id,
        refusal="approval_policy",
        worker_id="worker-supervisor",
    )
    db_session.commit()
    assert parked == [task.id]

    _set_rls_user(db_session, owner.id)
    await run_task_execution_job(
        db_session, job.id, owner.id, worker_id="worker-v01", lease_generation=generation, lease_seconds=120
    )
    db_session.expire_all()

    assert db_session.get(MainAIJob, job.id).status == "failed"
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.blocked
    assert task.blocker_reason == SUPERVISOR_REFUSAL_PARK_BLOCKER
    assert task.next_retry_at is None


@pytest.mark.asyncio
async def test_rerunning_the_fence_on_a_terminal_job_is_a_clean_noop(db_session, superuser_db, make_verified_user):
    """Restart/duplicate-delivery safety: the fence is not re-applied to a terminal job."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task, job, generation = _dispatch_and_claim(db_session, superuser_db, owner.id, goal)
    _set_rls_user(db_session, owner.id)
    await run_task_execution_job(
        db_session, job.id, owner.id, worker_id="worker-v01", lease_generation=generation, lease_seconds=120
    )
    db_session.expire_all()

    before = _task_event_count(db_session, task.id, MainAITaskEventType.verification_failed)
    job_events_before = len(
        db_session.execute(select(MainAIJobEvent).where(MainAIJobEvent.job_id == job.id)).scalars().all()
    )

    # A redelivered claim for the same (now terminal) job must not run or re-finalize it.
    await run_task_execution_job(
        db_session, job.id, owner.id, worker_id="worker-v01", lease_generation=generation, lease_seconds=120
    )
    db_session.expire_all()

    assert _task_event_count(db_session, task.id, MainAITaskEventType.verification_failed) == before
    assert (
        len(db_session.execute(select(MainAIJobEvent).where(MainAIJobEvent.job_id == job.id)).scalars().all())
        == job_events_before
    )
    assert db_session.get(MainAIJob, job.id).status == "failed"


# ------------------------------------------------------------------ concurrency / lease expiry


def _make_session():
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    return sessionmaker(bind=migration_engine)()


def test_two_concurrent_claims_of_a_leftover_governed_job_yield_exactly_one(db_session, superuser_db, make_verified_user):
    """Real threads, real connections: the leftover job can only ever be claimed once."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task = db_session.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-supervisor")
    db_session.commit()

    claims: list = []
    lock = threading.Lock()

    def _claim(worker_id: str) -> None:
        session = _make_session()
        try:
            result = claim_next_mainai_job(session, worker_id, 120)
            with lock:
                claims.append((worker_id, result))
        finally:
            session.close()

    threads = [threading.Thread(target=_claim, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)

    winners = [(worker_id, result) for worker_id, result in claims if result is not None]
    assert len(winners) == 1
    winner, (job_id, _, _) = winners[0]
    assert job_id == job.id

    # Exactly one durable claim generation was consumed, and the job is still running on the
    # winner -- never handed to two workers.
    row = superuser_db.execute(
        sa_text("SELECT status, locked_by FROM mainai_jobs WHERE id = :id"), {"id": str(job.id)}
    ).one()
    assert row[0] == "running"
    assert row[1] == winner


def test_an_expired_governed_task_execution_job_is_never_blindly_reclaimed(db_session, superuser_db, make_verified_user):
    """A dead governed job goes through the real recovery gate (which declines V0.1), never
    back into the generic claim loop."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task = db_session.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-supervisor")
    db_session.commit()
    assert claim_next_mainai_job(superuser_db, "worker-a", 120) is not None

    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = :id"),
        {"id": str(job.id)},
    )
    superuser_db.commit()

    assert claim_next_mainai_job(superuser_db, "worker-b", 120) is None
    row = superuser_db.execute(
        sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(job.id)}
    ).one()
    assert row[0] == "running"


# ------------------------------------------------------------------ controlled resumption


def test_re_authorizing_the_envelope_wakes_a_parked_task_exactly_once(db_session, superuser_db, make_verified_user):
    from app.development_supervisor.production_entry import park_tasks_after_supervisor_refusal

    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    first_envelope = _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task = db_session.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-supervisor")
    db_session.commit()

    park_tasks_after_supervisor_refusal(
        db_session,
        goal=goal,
        attempted=[(task.id, job.id)],
        execution_envelope_id=first_envelope.id,
        refusal="approval_policy",
        worker_id="worker-a",
    )
    db_session.commit()
    assert is_supervisor_refusal_park(db_session, task=db_session.get(MainAITask, task.id))

    # Same envelope is still current: no wake, no loop.
    assert (
        wake_tasks_blocked_for_supervisor_refusal(
            db_session, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=first_envelope.id
        )
        == []
    )
    db_session.commit()

    # A founder re-authorizes, and the next Supervisor tick reconciles the park (see
    # run_authorized_goal_supervisor_tick): exactly one controlled retry.
    second_envelope = _authorize(db_session, owner.id, goal.id)
    db_session.commit()
    woken = wake_tasks_blocked_for_supervisor_refusal(
        db_session, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=second_envelope.id
    )
    db_session.commit()
    assert [task_row.id for task_row in woken] == [task.id]
    db_session.expire_all()

    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.ready
    assert task.blocker_reason is None
    wake_event = (
        db_session.execute(
            select(MainAITaskEvent)
            .where(
                MainAITaskEvent.task_id == task.id,
                MainAITaskEvent.event_type == MainAITaskEventType.retry_scheduled,
            )
            .order_by(MainAITaskEvent.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert wake_event is not None
    assert wake_event.detail["reason"] == "supervisor_refusal_envelope_reauth_wake"
    assert wake_event.detail["execution_envelope_id"] == str(second_envelope.id)

    # The wake is one-shot: the same envelope cannot wake anything a second time.
    assert (
        wake_tasks_blocked_for_supervisor_refusal(
            db_session, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=second_envelope.id
        )
        == []
    )


def test_a_park_records_the_reason_and_the_envelope_that_refused_it(db_session, superuser_db, make_verified_user):
    from app.development_supervisor.production_entry import park_tasks_after_supervisor_refusal

    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(db_session, owner.id)
    envelope = _authorize(db_session, owner.id, goal.id)
    db_session.commit()

    task = db_session.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-supervisor")
    db_session.commit()

    park_tasks_after_supervisor_refusal(
        db_session,
        goal=goal,
        attempted=[(task.id, job.id)],
        execution_envelope_id=envelope.id,
        refusal="approval_policy mismatch",
        worker_id="worker-a",
    )
    db_session.commit()

    event = (
        db_session.execute(
            select(MainAITaskEvent)
            .where(
                MainAITaskEvent.task_id == task.id,
                MainAITaskEvent.event_type == MainAITaskEventType.blocked,
            )
            .order_by(MainAITaskEvent.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert event is not None
    assert event.detail["reason"] == SUPERVISOR_REFUSAL_PARK_REASON
    assert event.detail["execution_envelope_id"] == str(envelope.id)
    assert event.detail["mainai_job_id"] == str(job.id)
    assert "approval_policy" in event.detail["refusal"]
    assert db_session.get(MainAIGoal, goal.id).status is not None
