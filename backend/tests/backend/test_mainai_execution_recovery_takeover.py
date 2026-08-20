"""V0.2 recovery pipeline stage 5: takeover (recovery_takeover.execute_takeover()). Real
Postgres, real dispatch/claim cycle -- proves the FULL chain end to end: a dead job's task
moves back to `ready`, a genuinely new `mainai_jobs` row is dispatched, salvage copies durable
evidence forward, the dead job is fenced (`superseded`), and a worker can then actually claim
and run the NEW job to completion using the salvaged evidence (never recomputing a checkpoint
that already existed)."""

from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import JobLeaseLostError, JobNotSupersedableError, claim_next_mainai_job, mark_job_superseded, renew_mainai_job_lease
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import latest_checkpoint_for_step, record_checkpoint
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import TakeoverError, execute_takeover
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
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


def _fake_chat(response_text: str, call_counter: list[int] | None = None):
    async def _chat(self, messages, model, **kwargs):
        if call_counter is not None:
            call_counter[0] += 1
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="Takeover test goal", original_instruction="x", created_by="test")


def _task_and_dead_job(db_session, superuser_db, owner_id, *, task_type="read_only_audit") -> tuple[MainAITask, MainAIGoal]:
    """Dispatches a task, claims its job, then kills it (lease expired) -- the exact state a
    real takeover is meant to recover from."""
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="do it", task_type=task_type, verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    db_session.refresh(task)

    claim_next_mainai_job(superuser_db, "dead-worker", 120)
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(task.mainai_job_id)}
    )
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)
    db_session.refresh(task)
    return task, goal


async def _through_classification(db_session, task, dead_job):
    record = get_or_create_recovery_record(db_session, task=task, job=dead_job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=dead_job, record=record)
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    return record


@pytest.mark.asyncio
async def test_takeover_full_chain_nothing_done(db_session, superuser_db, owner_id):
    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.nothing_done

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert record.status == MainAIRecoveryStatus.completed
    assert record.takeover_job_id == new_job.id
    assert new_job.id != dead_job_id
    assert new_job.status == MainAIJobStatus.queued
    assert new_job.lease_generation == 0  # fresh, never yet claimed

    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status, superseded_by_job_id, completed_at FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "superseded"
    assert dead_row[1] == new_job.id
    assert dead_row[2] is not None

    db_session.refresh(task)
    assert task.mainai_job_id == new_job.id
    assert task.status == MainAITaskStatus.running  # dispatch_ready_task already claimed it back to running

    event_types = [
        row[0] for row in db_session.execute(
            sa_text("SELECT event_type FROM mainai_recovery_events WHERE recovery_record_id = :id ORDER BY created_at"), {"id": str(record.id)}
        ).all()
    ]
    assert event_types == [
        "dead_detected", "recovery_started", "recovery_inspected", "recovery_classified",
        "takeover_started", "salvage_started", "salvage_completed", "takeover_completed",
    ]


@pytest.mark.asyncio
async def test_takeover_salvages_checkpoint_so_the_new_job_never_recomputes_it(db_session, superuser_db, owner_id, monkeypatch):
    call_counter = [0]
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Analysen visar inga problem.", call_counter))

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)

    # The dead worker DID finish its real (mocked) AI call before dying -- a genuine
    # work_result checkpoint exists for the dead job, nothing else.
    record_checkpoint(db_session, task=task, goal=goal, job_id=dead_job_id, step="work_result", data={"work_result": {"analysis": "already done"}})
    db_session.commit()

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.checkpointed_work

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    copied = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="work_result")
    assert copied is not None
    assert copied.executor_state["work_result"] == {"analysis": "already done"}

    # A real worker now claims and runs the NEW job to completion -- the salvaged checkpoint
    # must be what it uses; the AI provider must NEVER be called again for this task.
    _, _, generation = claim_next_mainai_job(superuser_db, "recovery-worker-2", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_counter[0] == 0  # never recomputed
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_takeover_salvages_verification_so_the_new_job_never_reverifies(db_session, superuser_db, owner_id, monkeypatch):
    """V0.2 duplicate-side-effect prevention (verification): a dead job that already durably
    recorded BOTH a work_result checkpoint AND a passed verification checkpoint (VERIFIED_WORK)
    must have the new attempt reuse the verification verdict, never call verify_task() again --
    that would silently re-run the task's real verification_plan side effects (e.g. a real
    `python -m pytest` subprocess for a targeted_tests step) a second time against content that
    already, provably, passed."""
    import app.mainai_execution.execution_job as execution_job_module

    call_counter = [0]
    real_verify_task = execution_job_module.verify_task

    def _counting_verify_task(*args, **kwargs):
        call_counter[0] += 1
        return real_verify_task(*args, **kwargs)

    monkeypatch.setattr(execution_job_module, "verify_task", _counting_verify_task)

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)

    # The dead worker DID finish its real work AND its real verification before dying -- both
    # are durably recorded for the dead job, nothing else.
    record_checkpoint(db_session, task=task, goal=goal, job_id=dead_job_id, step="work_result", data={"work_result": {"summary": "already done"}})
    record_checkpoint(
        db_session, task=task, goal=goal, job_id=dead_job_id, step="verification",
        data={"verification": {"passed": True, "steps": []}},
    )
    db_session.add(MainAITaskEvent(task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.verification_passed, detail={}))
    db_session.commit()

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.verified_work

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    copied = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="verification")
    assert copied is not None
    assert copied.executor_state["verification"] == {"passed": True, "steps": []}

    _, _, generation = claim_next_mainai_job(superuser_db, "recovery-worker-2", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_counter[0] == 0  # verify_task() never called again for this task
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_takeover_salvages_open_pr_work_result_so_the_new_job_never_recreates_the_pr(db_session, superuser_db, owner_id, monkeypatch):
    """V0.2 duplicate-side-effect prevention (PR): an `open_pr` task's real work IS its
    checkpointed work_result (the actual `pull_request_number`/`pull_request_url` -- see
    execution_job.py's `_handle_open_pr()`). A dead `open_pr` job that already durably recorded
    that checkpoint must have the new attempt reuse it verbatim, never call
    GitHubClient.create_pull_request() again -- GitHub itself would reject a genuine second
    call with a 422 (only one open PR per head/base pair), but the point here is that it must
    never even be ATTEMPTED for work that's already durably done."""
    from app.integrations.github_client import GitHubClient

    call_counter = [0]

    async def _counting_create_pr(self, *, title, body, head, base):
        call_counter[0] += 1
        raise AssertionError("create_pull_request() must never be called for salvaged open_pr work")

    monkeypatch.setattr(GitHubClient, "create_pull_request", _counting_create_pr)

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id, task_type="open_pr")
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)

    # The dead worker DID open the real PR before dying -- its work_result checkpoint durably
    # records that outcome, nothing else.
    record_checkpoint(
        db_session, task=task, goal=goal, job_id=dead_job_id, step="work_result",
        data={"work_result": {"proposed": False, "pull_request_number": 42, "pull_request_url": "https://github.com/test-owner/test-repo/pull/42"}},
    )
    db_session.commit()

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.checkpointed_work

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    copied = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=new_job.id, step="work_result")
    assert copied is not None
    assert copied.executor_state["work_result"]["pull_request_number"] == 42

    _, _, generation = claim_next_mainai_job(superuser_db, "recovery-worker-2", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="recovery-worker-2", lease_generation=generation, lease_seconds=120)

    assert call_counter[0] == 0  # create_pull_request() never called again for this task
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_takeover_refuses_a_record_that_is_not_classified(db_session, superuser_db, owner_id):
    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    record = get_or_create_recovery_record(db_session, task=task, job=dead_job)
    db_session.commit()

    with pytest.raises(TakeoverError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")


@pytest.mark.asyncio
async def test_takeover_refuses_a_non_auto_salvageable_classification(db_session, superuser_db, owner_id):
    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    record = await _through_classification(db_session, task, dead_job)

    # Force an unsafe classification directly (simulating what a real CONFLICTED_STATE
    # classify() run would produce) to prove the takeover-side guard, independent of the
    # inspector/classifier's own already-tested detection logic.
    from app.models.mainai_recovery import RecoveryClassification as RC

    record.classification = RC.conflicted_state
    record.manual_review_required = True
    db_session.add(record)
    db_session.commit()

    with pytest.raises(TakeoverError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")


# ---------------------------------------------------------------- approval gate


@pytest.mark.asyncio
async def test_takeover_refuses_pushed_no_pr_without_founder_approval(db_session, superuser_db, owner_id):
    """V0.2 approval model: PUSHED_NO_PR means the dead attempt's code is already visible on
    GitHub -- a real external side effect -- so takeover must not proceed autonomously without
    an explicit founder approval, even though the classification is itself auto-salvageable."""
    from app.mainai_execution.recovery_approval import RecoveryApprovalRequiredError
    from app.models.mainai_recovery import RecoveryClassification as RC

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    record = await _through_classification(db_session, task, dead_job)

    # Force the classification directly (same established pattern as
    # test_takeover_refuses_a_non_auto_salvageable_classification above) to prove the
    # takeover-side approval guard independent of the inspector/classifier's own real-git
    # PUSHED_NO_PR detection, which is already covered by test_mainai_execution_recovery.py.
    record.classification = RC.pushed_no_pr
    db_session.add(record)
    db_session.commit()

    with pytest.raises(RecoveryApprovalRequiredError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")

    # Refused BEFORE any mutation -- the dead job's task must still be untouched (running),
    # never reset to ready, and no new job dispatched.
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.running
    assert task.mainai_job_id == dead_job.id


@pytest.mark.asyncio
async def test_takeover_refuses_pr_exists_without_founder_approval(db_session, superuser_db, owner_id):
    from app.mainai_execution.recovery_approval import RecoveryApprovalRequiredError
    from app.models.mainai_recovery import RecoveryClassification as RC

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    record = await _through_classification(db_session, task, dead_job)
    record.classification = RC.pr_exists
    db_session.add(record)
    db_session.commit()

    with pytest.raises(RecoveryApprovalRequiredError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")


@pytest.mark.asyncio
async def test_takeover_proceeds_for_pushed_no_pr_once_founder_approval_is_granted(db_session, superuser_db, owner_id):
    from app.mainai_execution.recovery_approval import grant_recovery_approval
    from app.models.mainai_recovery import RecoveryClassification as RC

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)
    record = await _through_classification(db_session, task, dead_job)
    record.classification = RC.pushed_no_pr
    db_session.add(record)
    db_session.commit()

    grant_recovery_approval(db_session, record=record, approved_by="founder@lifeos.local")
    db_session.commit()

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert record.status == MainAIRecoveryStatus.completed
    assert new_job.id != dead_job_id
    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "superseded"


def test_mark_job_superseded_refuses_a_job_that_is_not_genuinely_dead(db_session, superuser_db, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    claim_next_mainai_job(superuser_db, "healthy-worker", 120)  # still well within its lease

    import uuid

    with pytest.raises(JobNotSupersedableError):
        mark_job_superseded(superuser_db, job_id=job.id, superseded_by_job_id=uuid.uuid4())
    superuser_db.rollback()

    row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(job.id)}).one()
    assert row[0] == "running"  # untouched


def test_mark_job_superseded_accepts_already_terminal_job_during_takeover(
    db_session, superuser_db, owner_id
):
    """Terminal-without-finalize fence: job failed, recovery record at taking_over."""
    from app.models.mainai_recovery import MainAIRecoveryRecord, MainAIRecoveryStatus

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    dead_job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    claim_next_mainai_job(superuser_db, "dead-worker", 120)

    superuser_db.execute(
        sa_text(
            """
            UPDATE mainai_jobs
            SET status = 'failed', completed_at = now(), error_category = 'unexpected'
            WHERE id = :id
            """
        ),
        {"id": str(dead_job.id)},
    )
    # Replacement job must exist for the FK on superseded_by_job_id.
    replacement = executor.dispatch_ready_task(
        db_session,
        task=executor.reset_task_for_takeover(db_session, task=task),
        goal=goal,
        dispatched_by="recovery-test",
    )
    db_session.add(
        MainAIRecoveryRecord(
            task_id=task.id,
            owner_id=owner_id,
            job_id=dead_job.id,
            status=MainAIRecoveryStatus.taking_over,
            detected_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    mark_job_superseded(superuser_db, job_id=dead_job.id, superseded_by_job_id=replacement.id)
    superuser_db.commit()

    row = superuser_db.execute(
        sa_text("SELECT status, superseded_by_job_id FROM mainai_jobs WHERE id = :id"),
        {"id": str(dead_job.id)},
    ).one()
    assert row[0] == "superseded"
    assert str(row[1]) == str(replacement.id)


def test_mark_job_superseded_refuses_already_terminal_job_without_takeover_in_flight(
    db_session, superuser_db, owner_id
):
    """A honestly-failed job with no taking_over recovery record must NOT be rewritable."""
    import uuid

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    claim_next_mainai_job(superuser_db, "dead-worker", 120)

    superuser_db.execute(
        sa_text(
            """
            UPDATE mainai_jobs
            SET status = 'failed', completed_at = now(), error_category = 'unexpected'
            WHERE id = :id
            """
        ),
        {"id": str(job.id)},
    )
    superuser_db.execute(
        sa_text("UPDATE mainai_tasks SET status = 'failed', completed_at = now() WHERE id = :id"),
        {"id": str(task.id)},
    )
    superuser_db.commit()

    with pytest.raises(JobNotSupersedableError):
        mark_job_superseded(superuser_db, job_id=job.id, superseded_by_job_id=uuid.uuid4())
    superuser_db.rollback()

    row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(job.id)}).one()
    assert row[0] == "failed"


@pytest.mark.asyncio
async def test_superseded_job_writes_are_rejected_exactly_like_any_stale_lease(db_session, superuser_db, owner_id):
    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)
    stale_generation = dead_job.lease_generation

    record = await _through_classification(db_session, task, dead_job)
    await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(db_session, dead_job_id, "dead-worker", stale_generation, 120)


# ---------------------------------------------------------------- final report integration


@pytest.mark.asyncio
async def test_final_report_surfaces_recovery_history_for_a_recovered_task(db_session, superuser_db, owner_id):
    """V0.2 final-report integration: generate_goal_report() must show that a task went
    through a real dead-agent recovery -- durable evidence, not something a founder has to
    already know to go look for in a separate table."""
    from app.mainai_execution import final_report

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job_id = task.mainai_job_id
    dead_job = db_session.get(MainAIJob, dead_job_id)

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.nothing_done

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    report = final_report.generate_goal_report(db_session, goal_id=goal.id)
    task_report = report["tasks"][0]

    assert len(task_report["recovery_history"]) == 1
    entry = task_report["recovery_history"][0]
    assert entry["recovery_record_id"] == str(record.id)
    assert entry["dead_job_id"] == str(dead_job_id)
    assert entry["classification"] == "NOTHING_DONE"
    assert entry["status"] == "completed"
    assert entry["takeover_job_id"] == str(new_job.id)
    assert entry["manual_review_required"] is False

    assert report["summary"]["tasks_with_recovery_history"] == 1
    assert report["summary"]["recovery_attempts_total"] == 1


@pytest.mark.asyncio
async def test_final_report_flags_unresolved_risk_for_a_task_stuck_behind_a_blocked_recovery(db_session, superuser_db, owner_id):
    """A recovery record left `blocked`/manual_review_required (CONFLICTED_STATE/
    UNSAFE_TO_AUTO_RECOVER, or simply not yet inspected past that point) leaves the task's own
    status at `running` -- inspect/classify() never touches MainAITask.status. Without the
    recovery-aware unresolved_risk check, a task silently stuck forever behind a dead job a
    human hasn't reviewed would report as if nothing were wrong."""
    from app.mainai_execution import final_report
    from app.models.mainai_recovery import RecoveryClassification as RC

    task, goal = _task_and_dead_job(db_session, superuser_db, owner_id)
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    record = await _through_classification(db_session, task, dead_job)
    record.classification = RC.conflicted_state
    record.manual_review_required = True
    record.status = MainAIRecoveryStatus.blocked
    record.blocker = "genuine_divergence_between_local_and_remote"
    db_session.add(record)
    db_session.commit()

    report = final_report.generate_goal_report(db_session, goal_id=goal.id)
    task_report = report["tasks"][0]

    assert task_report["task_outcome"] == "running"  # untouched by inspect/classify
    assert task_report["unresolved_risk"] is True  # but the recovery history must catch it
    assert report["summary"]["unresolved_risk_count"] == 1
