"""Stream 7, item 6: confirm audit records are correct and COMPLETE for the failure and
cancellation paths, not just present -- "complete" here means the TASK itself (not just the
job row) reaches a truthful terminal state, since a founder reading the audit trail after a
failure expects the task's own status to explain what happened, not to find it still `running`
forever next to a job the log says already failed.

Found while verifying this (not assumed): `chat_with_fallback()`'s final, every-provider-failed
outcome is a raw `ProviderError` (app/providers/base.py), which is NOT one of the exception
types `run_task_execution_job()`'s own except clause catches
(`TaskExecutionError, VerificationStepError, GitHubClientError, WorktreeError`) -- so it
escapes that function entirely and is caught instead by `process_claimed_mainai_job()`'s outer,
deliberately generic `except Exception` in app/worker.py, whose own docstring says it exists
"to catch anything that escapes it unexpectedly (a bug, not a designed failure path)". That
outer handler marks the JOB failed but never finalizes the TASK -- unlike every in-function
failure path, which does both as one atomic commit. The result: a real, everyday "every AI
provider is down" failure left the task stuck at `running` permanently, with no further
mechanism that ever revisits it (the job is already a terminal `failed` row, so it can never be
reclaimed either) -- confirmed by the first test below before the fix, which failed; the fix
adds `ProviderError` to the caught tuple so this now-common failure follows the exact same
finalize-task+mark_failed path every other task-level failure already does."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.jobs.service import request_cancel
from app.mainai_execution import executor, planner
from app.mainai_execution.execution_job import run_task_execution_job
from app.models.audit import AuditLog
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobEventType
from app.providers.base import ProviderError
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


def _dispatch_read_only_audit(db_session, owner_id):
    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="Audit-completeness test",
        original_instruction="Audit something.", created_by="test",
    )
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    return task, job


def _job_events(db_session, job_id):
    return db_session.execute(
        sa_text("SELECT event_type FROM mainai_job_events WHERE job_id = :jid ORDER BY created_at"),
        {"jid": str(job_id)},
    ).scalars().all()


@pytest.mark.asyncio
async def test_a_total_provider_failure_finalizes_the_task_not_just_the_job(
    db_session, superuser_db, owner_id, monkeypatch
):
    """Every configured provider failing (chat_with_fallback()'s real, final-failure shape --
    not a bug, not a mock misconfiguration) must leave BOTH the job and the task in a truthful
    terminal state, with exactly one audit row recording it."""

    async def _always_fails(self, messages, model, **kwargs):
        raise ProviderError("provider unreachable", category="unreachable")

    monkeypatch.setattr(OpenAIProvider, "chat", _always_fails)

    task, job = _dispatch_read_only_audit(db_session, owner_id)
    job_id, _owner, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    superuser_db.commit()
    _set_rls_user(db_session, owner_id)

    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    db_session.refresh(task)
    # _finalize_task_outcome(passed=False) correctly classifies a provider-unreachable failure
    # as retryable (not permanently "failed") -- the real, intended terminal state here; the
    # defect this test guards against was the task never reaching ANY terminal state at all.
    assert task.status == MainAITaskStatus.retryable_failed, (
        "the task must reach a truthful terminal state when its only AI call fails -- "
        "not be left running forever next to an already-terminal job"
    )

    refreshed_job = superuser_db.get(MainAIJob, job_id)
    assert refreshed_job.status.value == "failed"

    rows = superuser_db.execute(
        select(AuditLog).where(AuditLog.entity_type == "mainai_job", AuditLog.entity_id == str(job_id), AuditLog.action == "mainai_job_failed")
    ).scalars().all()
    assert len(rows) == 1, f"expected exactly one mainai_job_failed audit row, found {len(rows)}"

    events = _job_events(superuser_db, job_id)
    assert events.count("failed") == 1, f"expected exactly one 'failed' job event, found {events.count('failed')} in {events}"


@pytest.mark.asyncio
async def test_a_cooperative_cancellation_produces_exactly_one_audit_row_and_event_sequence(
    db_session, superuser_db, owner_id
):
    """A founder-requested cancellation, acknowledged at the dispatch-time safe checkpoint,
    must leave exactly one cancel_requested event/audit pairing and exactly one terminal
    cancelled event/audit pairing -- no duplicates, no missing rows, on both the job and the
    task side."""
    task, job = _dispatch_read_only_audit(db_session, owner_id)
    job_id, _owner, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    superuser_db.commit()

    request_cancel(superuser_db, job_id, requested_by=owner_id)

    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    db_session.refresh(task)
    from app.models.mainai_execution import MainAITaskStatus as _Status

    assert task.status == _Status.cancelled

    refreshed_job = superuser_db.get(MainAIJob, job_id)
    assert refreshed_job.status.value == "cancelled"
    assert refreshed_job.cancel_acknowledged is True

    audit_actions = superuser_db.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "mainai_job", AuditLog.entity_id == str(job_id))
    ).scalars().all()
    assert audit_actions.count("mainai_job_cancel_requested") == 1
    assert audit_actions.count("mainai_job_cancelled") == 1

    events = _job_events(superuser_db, job_id)
    assert events.count(MainAIJobEventType.cancel_requested.value) == 1
    assert events.count(MainAIJobEventType.cancel_acknowledged.value) == 1
    assert events.count(MainAIJobEventType.cancelled.value) == 1
