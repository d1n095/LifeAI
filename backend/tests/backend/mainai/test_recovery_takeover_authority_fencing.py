"""RECOVERY MUST NEVER INCREASE OR BYPASS AUTHORITY.

`app/mainai_execution/recovery_takeover.py`'s `execute_takeover()` is the ONE place a dead
`task_execution` job's ownership can be transferred to a new attempt -- both the automatic
`app.worker.py._advance_mainai_execution_auto_recovery()` tick and a founder's own
`POST /tasks/{id}/recover` (app/routers/mainai_execution.py) call it, and only it. Its
existing takeover mechanism (`reset_task_for_takeover()` + `dispatch_ready_task()`) resumes a
task through V0.1's own generic, envelope-blind executor
(`app.mainai_execution.execution_job.run_task_execution_job()` -- zero references to
`OperatorContext`/`SupervisorScope`/`allowed_paths` anywhere in that module) -- exactly the
same `dispatch_ready_task()` call PR #154 already fixed `app.worker.py`'s
`_advance_mainai_execution_tasks()` to never make for a goal that has ever been
execution-authorization-envelope-governed. That fix lives entirely in
`_advance_mainai_execution_tasks()`; `execute_takeover()` is a second, independent caller of
the same `dispatch_ready_task()` and was never covered by it (see PR #157's own explicit
"Residual" note: "`execute_takeover` / auto-recovery still envelope-blind").

This file proves:
  - A Supervisor-governed task whose job dies is NEVER resumed through V0.1's dispatch --
    regardless of whether the goal's envelope is currently active, revoked, or superseded by
    a narrower one (the EVER_GOVERNED fact alone is what matters, not the current envelope).
  - A never-governed goal's takeover is completely unaffected (regression).
  - The existing approval gate (PUSHED_NO_PR/PR_EXISTS) still runs BEFORE the governance
    check and is never weakened or bypassed by it, in either direction.
  - The new terminal-job-fencing primitive (`mark_job_failed_after_governed_recovery_decline`)
    only ever writes to a genuinely dead job, mirroring `mark_job_superseded`'s own three
    proven cases.
  - The REAL production path composes correctly end to end: a governed task's job crashes,
    the automatic recovery tick declines to V0.1-resume it, and the goal's OWN next Supervisor
    tick rediscovers and re-governs it -- never a second, independent authority
    reconstruction invented in the recovery pipeline itself.
  - FIRST-GOVERNANCE TOCTOU: a founder authorizing a goal's FIRST-EVER envelope and a
    concurrent dead-job recovery pass for that SAME goal are genuinely serialized by a shared
    goal-row lock (`SELECT ... FOR UPDATE` in both `execute_takeover()` and
    `authorize_execution_scope()`), not just re-checked with another ordinary SELECT. Both
    real threads, both real DB connections, real blocking proven with a timeout -- not a
    single-threaded simulation."""

import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.execution_envelopes.service import goal_has_ever_been_envelope_governed
from app.jobs.mainai_job_lease import (
    JobNotDeclinableError,
    claim_next_mainai_job,
    mark_job_failed_after_governed_recovery_decline,
)
from app.mainai_execution import executor, planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_approval import RecoveryApprovalRequiredError, grant_recovery_approval
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import TakeoverError, execute_takeover
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAIRecoveryRecord, MainAIRecoveryStatus, RecoveryClassification
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


def _goal(db_session, owner_id, title="recovery authority fencing test"):
    return planner.create_goal(db_session, owner_id=owner_id, title=title, original_instruction="x", created_by="test")


def _authorize(db_session, owner_id, goal_id, *, authorized_paths=None, authorized_capabilities=None):
    proposal = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"rtaf-prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db_session, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=authorized_paths if authorized_paths is not None else ["README.md"],
        authorized_capabilities=authorized_capabilities if authorized_capabilities is not None else ["read_file"],
        authorized_risk="low", envelope_idempotency_key=f"rtaf-env-{uuid.uuid4()}",
    )
    return envelope


def _task_and_dead_job(db_session, superuser_db, owner_id, goal, *, task_type="read_only_audit") -> tuple[MainAITask, MainAIJob]:
    """Dispatches a task under `goal`, claims its job, then kills the lease -- the exact
    TaskLiveness.dead state a real recovery pass is meant to act on. Mirrors
    tests/backend/test_mainai_execution_recovery_takeover.py's own `_task_and_dead_job()`."""
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[PlannedTaskSpec(description="do it", task_type=task_type, verification_plan=[])],
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
    dead_job = db_session.get(MainAIJob, task.mainai_job_id)
    return task, dead_job


async def _through_classification(db_session, task, dead_job):
    record = get_or_create_recovery_record(db_session, task=task, job=dead_job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=dead_job, record=record)
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    return record


def _events(db_session, record_id):
    return [
        row[0] for row in db_session.execute(
            sa_text("SELECT event_type FROM mainai_recovery_events WHERE recovery_record_id = :id ORDER BY created_at"), {"id": str(record_id)}
        ).all()
    ]


# ---------------------------------------------------------------- core invariant: governed decline


@pytest.mark.asyncio
async def test_takeover_declines_v01_dispatch_for_a_goal_with_a_currently_active_envelope(db_session, superuser_db, owner_id):
    """The primary case: EVER_GOVERNED + CURRENT ACTIVE ENVELOPE. Negative control -- before
    this fix, execute_takeover() unconditionally called dispatch_ready_task() and this test's
    `new_job is None` assertion (and the dead job's `failed`-not-`superseded` status) would
    both fail against the prior code."""
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    dead_job_id = dead_job.id

    record = await _through_classification(db_session, task, dead_job)
    assert record.classification == RecoveryClassification.nothing_done

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert new_job is None  # no V0.1 job ever minted
    assert record.status == MainAIRecoveryStatus.completed
    assert record.takeover_job_id is None  # never a takeover, so nothing to record here
    assert record.takeover_executor is None

    db_session.refresh(task)
    assert task.status == MainAITaskStatus.ready  # no fabricated verdict, returned for governed redispatch
    assert task.mainai_job_id == dead_job_id  # unchanged -- no replacement minted by this call

    superuser_db.expire_all()
    dead_row = superuser_db.execute(
        sa_text("SELECT status, superseded_by_job_id, completed_at FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}
    ).one()
    assert dead_row[0] == "failed"  # not 'superseded' -- no successor job to link
    assert dead_row[1] is None
    assert dead_row[2] is not None

    assert _events(db_session, record.id) == [
        "dead_detected", "recovery_started", "recovery_inspected", "recovery_classified",
        "takeover_started", "takeover_declined_governed",
    ]


@pytest.mark.asyncio
async def test_takeover_still_declines_once_the_active_envelope_was_revoked_with_no_replacement(db_session, superuser_db, owner_id):
    """EVER_GOVERNED + NO ACTIVE ENVELOPE. Proves the decline is driven by the durable
    EVER_GOVERNED fact, never by "is there currently an active envelope" -- a revoked
    envelope must not silently reopen V0.1's wider path any more than an active one does."""
    goal = _goal(db_session, owner_id)
    envelope = _authorize(db_session, owner_id, goal.id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)

    # Real schema only has active/superseded (see migration 0057's own CHECK) -- "no active
    # envelope with no replacement" is modeled the same way PR #154's own tests do it
    # (test_advance_tasks_excludes_envelope_governed_goals.py), by superseding in place with
    # no successor row, not a dedicated 'revoked' status.
    envelope.status = "superseded"
    db_session.add(envelope)
    db_session.commit()

    record = await _through_classification(db_session, task, dead_job)
    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert new_job is None
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.ready


@pytest.mark.asyncio
async def test_takeover_still_declines_when_envelope_a_was_superseded_by_narrower_b(db_session, superuser_db, owner_id):
    """EVER_GOVERNED + ENVELOPE A SUPERSEDED BY B. The decline itself does not depend on which
    envelope is current -- it only needs the EVER_GOVERNED fact. B being the maximum authority
    for whatever picks the task up next is `eligible_authorized_goals()`'s own job (already
    proven by app.worker.py's own PR #154 tests and test_supervisor_production_entry.py's
    narrowing tests), not this function's."""
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id, authorized_paths=["README.md"], authorized_capabilities=["read_file", "patch_file"])
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)

    # Founder re-authorizes narrower -- supersedes A, becomes the new active envelope B.
    narrower_proposal = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"rtaf-narrow-prop-{uuid.uuid4()}")
    _, envelope_b = authorize_execution_scope(
        db_session, owner_id=owner_id, proposal_id=narrower_proposal.id, authorized_by="founder",
        authorized_paths=["README.md"], authorized_capabilities=["read_file"], authorized_risk="low",
        envelope_idempotency_key=f"rtaf-narrow-env-{uuid.uuid4()}",
    )
    db_session.commit()

    record = await _through_classification(db_session, task, dead_job)
    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert new_job is None
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.ready
    assert envelope_b.status == "active"  # B remains the maximum authority for whatever redispatches next


@pytest.mark.asyncio
async def test_takeover_still_performs_a_real_v01_takeover_for_a_never_governed_goal(db_session, superuser_db, owner_id):
    """Regression: a goal with NO execution-authorization-envelope row, ever, is completely
    unaffected -- the ordinary V0.1 takeover this whole pipeline was built for still works
    exactly as before."""
    goal = _goal(db_session, owner_id)
    db_session.commit()
    assert not goal_has_ever_been_envelope_governed(db_session, owner_id=owner_id, goal_id=goal.id)

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    dead_job_id = dead_job.id

    record = await _through_classification(db_session, task, dead_job)
    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert new_job is not None
    assert new_job.id != dead_job_id
    assert record.takeover_job_id == new_job.id
    db_session.refresh(task)
    assert task.mainai_job_id == new_job.id
    assert task.status == MainAITaskStatus.running  # V0.1's dispatch_ready_task claimed it back to running

    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "superseded"
    assert "takeover_completed" in _events(db_session, record.id)


@pytest.mark.asyncio
async def test_double_takeover_attempt_on_an_already_declined_governed_record_is_refused(db_session, superuser_db, owner_id):
    """Two recovery workers racing the same dead governed job: at most one outcome is ever
    recorded -- the second call finds `record.status != classified` (already advanced to
    `completed` by the first) and refuses via the same top-of-function guard every other
    takeover path already relies on. No duplicated decline event, no double effect."""
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    record = await _through_classification(db_session, task, dead_job)

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker-a")
    db_session.commit()
    assert new_job is None

    with pytest.raises(TakeoverError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker-b")


# ---------------------------------------------------------------- approval gate ordering


@pytest.mark.asyncio
async def test_approval_gate_still_blocks_pushed_no_pr_for_a_governed_goal_before_any_mutation(db_session, superuser_db, owner_id):
    """AUTHORITY BOUNDARY: the governance decline must never be reached in a way that skips
    the EXISTING PUSHED_NO_PR/PR_EXISTS approval gate -- that gate protects a completely
    different thing (a real external GitHub side effect already happened) and must keep
    firing first, exactly as it does for a never-governed goal."""
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    record = await _through_classification(db_session, task, dead_job)
    record.classification = RecoveryClassification.pushed_no_pr
    db_session.add(record)
    db_session.commit()

    with pytest.raises(RecoveryApprovalRequiredError):
        await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")

    db_session.refresh(task)
    assert task.status == MainAITaskStatus.running  # untouched -- refused before any mutation
    assert task.mainai_job_id == dead_job.id


@pytest.mark.asyncio
async def test_takeover_still_declines_for_a_governed_goal_even_once_pushed_no_pr_is_approved(db_session, superuser_db, owner_id):
    """AUTHORITY BOUNDARY, the other direction: a founder's `approval_granted` on the
    recovery-visibility gate is NOT authority to run under V0.1 -- it only clears "is it safe
    to let an autonomous pass look at a dead job with code already on GitHub", never
    "is this goal's authority envelope-scoped or not". Approval must not accidentally punch
    through the governance decline."""
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    record = await _through_classification(db_session, task, dead_job)
    record.classification = RecoveryClassification.pushed_no_pr
    db_session.add(record)
    db_session.commit()

    grant_recovery_approval(db_session, record=record, approved_by="founder@lifeos.local")
    db_session.commit()

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="recovery-worker")
    db_session.commit()

    assert new_job is None  # approval alone never grants V0.1 execution authority
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.ready


# ---------------------------------------------------------------- mark_job_failed_after_governed_recovery_decline


def test_mark_job_failed_after_governed_recovery_decline_refuses_a_job_that_is_not_genuinely_dead(db_session, superuser_db, owner_id):
    goal = _goal(db_session, owner_id)
    db_session.commit()
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    claim_next_mainai_job(superuser_db, "healthy-worker", 120)  # still well within its lease

    with pytest.raises(JobNotDeclinableError):
        mark_job_failed_after_governed_recovery_decline(superuser_db, job_id=job.id)
    superuser_db.rollback()

    row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(job.id)}).one()
    assert row[0] == "running"  # untouched


def test_mark_job_failed_after_governed_recovery_decline_accepts_terminal_without_finalize_during_takeover(db_session, superuser_db, owner_id):
    """Terminal-without-finalize fence: job already failed, recovery record at taking_over --
    mirrors mark_job_superseded's own equivalent proven case."""
    goal = _goal(db_session, owner_id)
    db_session.commit()
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    dead_job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    claim_next_mainai_job(superuser_db, "dead-worker", 120)

    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET status = 'failed', completed_at = now(), error_category = 'unexpected' WHERE id = :id"),
        {"id": str(dead_job.id)},
    )
    db_session.add(
        MainAIRecoveryRecord(
            task_id=task.id, owner_id=owner_id, job_id=dead_job.id,
            status=MainAIRecoveryStatus.taking_over, detected_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    mark_job_failed_after_governed_recovery_decline(superuser_db, job_id=dead_job.id)
    superuser_db.commit()

    row = superuser_db.execute(sa_text("SELECT status, superseded_by_job_id FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job.id)}).one()
    assert row[0] == "failed"
    assert row[1] is None


def test_mark_job_failed_after_governed_recovery_decline_refuses_terminal_job_without_takeover_in_flight(db_session, superuser_db, owner_id):
    goal = _goal(db_session, owner_id)
    db_session.commit()
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[PlannedTaskSpec(description="do it", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    claim_next_mainai_job(superuser_db, "dead-worker", 120)

    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET status = 'failed', completed_at = now(), error_category = 'unexpected' WHERE id = :id"),
        {"id": str(job.id)},
    )
    superuser_db.execute(sa_text("UPDATE mainai_tasks SET status = 'failed', completed_at = now() WHERE id = :id"), {"id": str(task.id)})
    superuser_db.commit()

    with pytest.raises(JobNotDeclinableError):
        mark_job_failed_after_governed_recovery_decline(superuser_db, job_id=job.id)
    superuser_db.rollback()


# ---------------------------------------------------------------- goal_has_ever_been_envelope_governed


def test_goal_has_ever_been_envelope_governed_is_false_for_a_goal_with_no_envelope(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    db_session.commit()
    assert goal_has_ever_been_envelope_governed(db_session, owner_id=owner_id, goal_id=goal.id) is False


def test_goal_has_ever_been_envelope_governed_is_true_with_an_active_envelope(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id)
    db_session.commit()
    assert goal_has_ever_been_envelope_governed(db_session, owner_id=owner_id, goal_id=goal.id) is True


def test_goal_has_ever_been_envelope_governed_stays_true_after_the_only_envelope_is_revoked(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    envelope = _authorize(db_session, owner_id, goal.id)
    # Real schema only has active/superseded (see migration 0057's own CHECK) -- "no active
    # envelope with no replacement" is modeled the same way PR #154's own tests do it
    # (test_advance_tasks_excludes_envelope_governed_goals.py), by superseding in place with
    # no successor row, not a dedicated 'revoked' status.
    envelope.status = "superseded"
    db_session.add(envelope)
    db_session.commit()
    assert goal_has_ever_been_envelope_governed(db_session, owner_id=owner_id, goal_id=goal.id) is True


# ---------------------------------------------------------------- real production path: automatic worker tick


@pytest.mark.asyncio
async def test_automatic_recovery_tick_never_hands_a_governed_dead_job_to_v01(db_session, superuser_db, owner_id):
    """Attacks the REAL production path, not just execute_takeover() in isolation:
    `Worker()._advance_mainai_execution_auto_recovery()` -- the unattended tick both
    `app.worker.py`'s own poll loop AND `_advance_mainai_execution_auto_recovery`'s docstring
    describe as the thing that finds dead task_execution jobs with zero founder action.
    A governed goal's dead job must come out of this tick exactly as it does from a direct
    execute_takeover() call -- returned to `ready`, never running under a fresh V0.1 job."""
    goal = _goal(db_session, owner_id)
    _authorize(db_session, owner_id, goal.id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    dead_job_id = dead_job.id

    worker = Worker()
    await worker._advance_mainai_execution_auto_recovery(superuser_db)

    record = superuser_db.query(MainAIRecoveryRecord).filter(MainAIRecoveryRecord.job_id == dead_job_id).one()
    assert record.classification == RecoveryClassification.nothing_done
    assert record.status == MainAIRecoveryStatus.completed
    assert record.takeover_job_id is None

    superuser_db.expire_all()
    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "failed"

    _set_rls_user(db_session, owner_id)
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.ready
    assert task.mainai_job_id == dead_job_id  # no V0.1 replacement was ever minted, unlike
    # the never-governed demo in test_mainai_execution_auto_recovery.py where this changes.


# ---------------------------------------------------------------- real production path: crash -> decline -> re-governed


@pytest.mark.asyncio
async def test_a_crashed_governed_task_is_declined_by_recovery_then_re_governed_by_the_next_supervisor_tick(
    db_session, superuser_db, make_verified_user, tmp_path, monkeypatch
):
    """The full composed real production path (Section '#159 clean-worktree composition' +
    the crash/recovery boundary together): a Supervisor-dispatched, real `mainai_jobs` row
    dies (lease expires) mid-flight. The automatic recovery tick declines to resume it via
    V0.1. The goal's OWN next `run_authorized_goal_supervisor_tick()` call -- exactly what
    `app.worker.py`'s `_advance_authorized_supervisor_goals` drives automatically on its own
    schedule -- rediscovers the now-`ready` task and dispatches/self-claims a genuinely NEW,
    governed job for it. One authoritative recovery path (Supervisor's own), never a second
    one duplicated inside the recovery pipeline."""
    import subprocess

    from app.development_supervisor import service as supervisor_service
    from app.development_supervisor.production_entry import run_authorized_goal_supervisor_tick
    import app.development_supervisor.production_entry as entry_module
    from app.safe_planner.service import CandidateStep, PlanCandidate, PlanningResult

    def _git(cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()

    import app.development_supervisor.production_worktree as worktree_module
    monkeypatch.setattr(worktree_module, "WORKTREE_ROOT", tmp_path / "supervisor-goal-worktrees")

    repo = tmp_path / "worker-source-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@test.local")
    _git(repo, "config", "user.name", "Worker")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(entry_module, "worker_source_repo_root", lambda: repo)

    owner, _password = make_verified_user()
    _set_rls_user(superuser_db, owner.id)
    goal = planner.create_goal(superuser_db, owner_id=owner.id, title="crash+recovery composition test", original_instruction="edit a file", created_by="test")
    planner.create_plan(
        superuser_db, goal=goal, rationale="single task",
        tasks=[PlannedTaskSpec(description="edit a file", task_type="repo_edit")],
        created_by="test",
    )
    superuser_db.flush()
    envelope = _authorize(superuser_db, owner.id, goal.id, authorized_paths=["README.md"], authorized_capabilities=["read_file", "patch_file"])
    superuser_db.commit()

    # Force a deferred (never-completing) outcome so the FIRST tick's dispatched job is left
    # genuinely `running` -- the exact same forced-defer technique
    # test_supervisor_production_entry.py's own resume tests already use.
    monkeypatch.setattr(
        supervisor_service, "plan_founder_request",
        lambda *_a, **_k: PlanningResult("CAPABILITY_MISSING", {"reason": "forced", "requested_capability": "inspect_git_history"}),
    )
    original_run = entry_module.run_supervisor

    async def _run_with_gap(db, *, scope, bindings, worker_id, bounds=None):
        from dataclasses import replace

        scope = replace(scope, provider_spend_authorized=True)
        forced = PlanCandidate("force", "force", "force", (CandidateStep("x", "x", "x", "inspect_git_history"),))
        bindings = tuple(replace(b, candidate=forced, independent=True) for b in bindings)
        return await original_run(db, scope=scope, bindings=bindings, worker_id=worker_id, bounds=bounds)

    monkeypatch.setattr(entry_module, "run_supervisor", _run_with_gap)

    result_1 = await run_authorized_goal_supervisor_tick(superuser_db, goal=goal, envelope=envelope, worker_id="worker-a")
    superuser_db.commit()
    assert result_1 is not None

    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    assert task.status == MainAITaskStatus.running
    first_job_id = task.mainai_job_id
    assert first_job_id is not None

    # Simulate a real crash: worker-a dies mid-action, the job's OWN mainai_jobs lease expires.
    # (The goal's supervisor_goal_lease is already free -- released in prepare_context()'s own
    # finally block at the end of every tick, per that function's own docstring.)
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(first_job_id)})
    superuser_db.commit()

    worker = Worker()
    await worker._advance_mainai_execution_auto_recovery(superuser_db)

    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.ready  # declined V0.1 resume, returned for real governance
    assert task.mainai_job_id == first_job_id  # no V0.1 replacement minted

    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(first_job_id)}).one()
    assert dead_row[0] == "failed"

    # The goal's own next Supervisor tick -- exactly what app.worker.py's
    # _advance_authorized_supervisor_goals drives automatically -- rediscovers the ready task
    # and dispatches/self-claims a genuinely NEW, governed job for it.
    result_2 = await run_authorized_goal_supervisor_tick(superuser_db, goal=goal, envelope=envelope, worker_id="worker-b")
    superuser_db.commit()
    assert result_2 is not None

    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    assert task.mainai_job_id is not None
    assert task.mainai_job_id != first_job_id  # a genuinely new job, minted by Supervisor itself
    new_job = superuser_db.get(MainAIJob, task.mainai_job_id)
    assert new_job.locked_by == "worker-b"  # governed dispatch self-claims, unlike V0.1's queued-for-anyone job


# ---------------------------------------------------------------- migration downgrade, with real data


@pytest.mark.asyncio
async def test_downgrading_past_this_migration_fails_loudly_once_a_real_row_exists_not_silently():
    """`alembic downgrade -1` (from migration 0061's own head at the time this test was
    written) re-adds the narrower CHECK constraint that predates 'takeover_declined_governed'.
    A no-data round-trip proves nothing about whether that is actually safe once real recovery
    history exists -- this test inserts a GENUINE row via the real execute_takeover() code
    path (not a hand-crafted INSERT), then actually runs `alembic downgrade -1` as a real
    subprocess.

    Must fail loudly (IntegrityError/CheckViolation), never silently succeed or silently drop/
    rewrite the row -- mainai_recovery_events is append-only at the database level (migration
    0033's trg_mainai_recovery_events_deny_mutation denies UPDATE unconditionally, for every
    role, with no GUC escape hatch), so there is no safe way for a downgrade to make room for
    the narrower constraint other than refusing.

    Runs against a genuinely ISOLATED, throwaway database -- never the shared session-scoped
    `db_session`/`superuser_db` database every other test in this pytest run also depends on.
    `alembic downgrade -1` is inherently a RELATIVE target: it always downgrades whatever the
    CURRENT actual head is, not necessarily migration 0061 specifically. Any later migration
    added on top of 0061 in the future (there already is one -- 0062) would make a bare
    `downgrade -1` against the shared database downgrade THAT migration instead, potentially
    dropping a table/tearing down state a later migration's own downgrade() is not designed to
    survive being called on real data, and -- worse -- silently corrupting schema state for
    every OTHER test that runs afterward in the same pytest session, since nothing else in the
    suite re-migrates the shared database mid-session. A dedicated, disposable database
    created/torn down entirely within this one test closes that class of bug structurally,
    regardless of how many further migrations get layered on top later."""
    import subprocess
    import uuid as uuid_module
    from pathlib import Path
    from urllib.parse import urlparse, urlunparse

    import psycopg2
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    backend_root = Path(__file__).resolve().parents[3]
    base_dsn = os.environ["DATABASE_URL"]
    parsed = urlparse(base_dsn)
    temp_db_name = f"lifeos_downgrade_probe_{uuid_module.uuid4().hex[:16]}"
    # Replace only the path (database name) and keep scheme/userinfo/host/port/query
    # byte-for-byte from the real DATABASE_URL -- rebuilding the DSN by hand from
    # hostname/port/username alone (as an earlier version of this test did) silently
    # drops any password, which passes locally under trust-auth Postgres but fails in
    # CI's password-auth container with "fe_sendauth: no password supplied".
    admin_dsn = urlunparse(parsed._replace(path="/postgres"))
    temp_dsn = urlunparse(parsed._replace(path=f"/{temp_db_name}"))

    admin_conn = psycopg2.connect(admin_dsn)
    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{temp_db_name}"')

        env = dict(os.environ)
        env["DATABASE_URL"] = temp_dsn
        upgraded = subprocess.run(
            ["alembic", "upgrade", "head"], cwd=str(backend_root), env=env, capture_output=True, text=True,
        )
        assert upgraded.returncode == 0, f"isolated probe DB failed to migrate to head:\n{upgraded.stdout}\n{upgraded.stderr}"
        current_before = subprocess.run(["alembic", "current"], cwd=str(backend_root), env=env, capture_output=True, text=True)
        assert "0061" in current_before.stdout  # this test only means anything against exactly 0061's own downgrade

        temp_engine = create_engine(temp_dsn)
        temp_session = sessionmaker(bind=temp_engine)()
        try:
            owner_id = uuid_module.uuid4()
            temp_session.execute(
                sa_text(
                    "INSERT INTO users (id, email, password_hash, role, is_active, created_at, email_verified, sessions_valid_after) "
                    "VALUES (:id, :email, 'x', 'founder', true, now(), true, now())"
                ),
                {"id": str(owner_id), "email": f"downgrade-probe-{owner_id}@test.local"},
            )
            temp_session.commit()

            goal = _goal(temp_session, owner_id)
            _authorize(temp_session, owner_id, goal.id)
            temp_session.commit()

            task, dead_job = _task_and_dead_job(temp_session, temp_session, owner_id, goal)
            record = await _through_classification(temp_session, task, dead_job)
            assert record.classification == RecoveryClassification.nothing_done

            record, new_job = await execute_takeover(temp_session, task=task, goal=goal, record=record, dispatched_by="downgrade-proof")
            temp_session.commit()
            assert new_job is None

            row = temp_session.execute(
                sa_text("SELECT event_type FROM mainai_recovery_events WHERE recovery_record_id = :id AND event_type = 'takeover_declined_governed'"),
                {"id": str(record.id)},
            ).one()
            assert row[0] == "takeover_declined_governed"  # the genuine row this downgrade must refuse to orphan
            # Close out this session's own transaction (even a read-only SELECT holds an
            # AccessShareLock until commit/rollback) -- otherwise it deadlocks against the
            # subprocess below's own `ALTER TABLE ... DROP CONSTRAINT`, which needs ACCESS
            # EXCLUSIVE.
            temp_session.commit()
        finally:
            temp_session.close()
            temp_engine.dispose()

        result = subprocess.run(
            ["alembic", "downgrade", "-1"], cwd=str(backend_root), env=env, capture_output=True, text=True,
        )
        assert result.returncode != 0, f"downgrade unexpectedly succeeded with real data present:\n{result.stdout}\n{result.stderr}"
        assert "CheckViolation" in result.stderr or "IntegrityError" in result.stderr or "violates check constraint" in result.stderr

        current_after = subprocess.run(["alembic", "current"], cwd=str(backend_root), env=env, capture_output=True, text=True)
        assert "0061" in current_after.stdout  # transactional DDL rollback -- schema genuinely unchanged
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (temp_db_name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{temp_db_name}"')
        admin_conn.close()


# ---------------------------------------------------------------- first-governance TOCTOU race


def _make_session():
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    return sessionmaker(bind=migration_engine)()


def test_first_governance_toctou_race_governance_committed_while_recovery_waits_is_observed(
    db_session, superuser_db, owner_id
):
    """Ordering B (acceptable): first governance wins -> recovery observes EVER_GOVERNED ->
    V0.1 takeover is declined. Two REAL threads, two REAL DB connections. Thread A takes the
    goal-row lock directly (simulating "some transaction is mid-flight, about to authorize
    the first envelope") and holds it past a controlled checkpoint; thread B runs the REAL
    `execute_takeover()` and must genuinely block on that same lock (proven by a join timeout,
    not assumed). Only once thread A actually creates and commits the envelope -- still
    holding the SAME already-acquired lock, so this is exactly authorize_execution_scope()'s
    own code path re-entering a lock its own transaction already holds, never a second lock --
    does thread B's execute_takeover() unblock. FORBIDDEN outcome this proves does NOT happen:
    thread B minting a new V0.1 job after thread A's envelope is already committed."""
    import asyncio
    import threading

    from app.models.mainai_execution import MainAIGoal

    goal = _goal(db_session, owner_id)
    db_session.commit()
    assert not goal_has_ever_been_envelope_governed(db_session, owner_id=owner_id, goal_id=goal.id)

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    record = asyncio.run(_through_classification(db_session, task, dead_job))
    assert record.classification == RecoveryClassification.nothing_done
    record_id, task_id, goal_id, dead_job_id = record.id, task.id, goal.id, dead_job.id
    db_session.commit()

    lock_held = threading.Event()
    release_lock = threading.Event()
    results: dict = {}

    def _thread_a_hold_then_authorize():
        session = _make_session()
        try:
            session.execute(sa_text("SELECT id FROM mainai_goals WHERE id = :id FOR UPDATE"), {"id": str(goal_id)})
            lock_held.set()
            release_lock.wait(timeout=10)
            proposal = propose_execution_scope(session, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"toctou-b-prop-{uuid.uuid4()}")
            _, envelope = authorize_execution_scope(
                session, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                authorized_paths=["README.md"], authorized_capabilities=["read_file"], authorized_risk="low",
                envelope_idempotency_key=f"toctou-b-env-{uuid.uuid4()}",
            )
            session.commit()
            results["envelope_id"] = envelope.id
        finally:
            session.close()

    def _thread_b_takeover():
        session = _make_session()
        try:
            goal_row = session.get(MainAIGoal, goal_id)
            task_row = session.get(MainAITask, task_id)
            record_row = session.get(MainAIRecoveryRecord, record_id)

            async def _run():
                return await execute_takeover(session, task=task_row, goal=goal_row, record=record_row, dispatched_by="toctou-recovery")

            record_out, new_job = asyncio.run(_run())
            session.commit()
            results["new_job_id"] = new_job.id if new_job is not None else None
        finally:
            session.close()

    thread_a = threading.Thread(target=_thread_a_hold_then_authorize)
    thread_a.start()
    assert lock_held.wait(timeout=10), "thread A never acquired the goal-row lock"

    thread_b = threading.Thread(target=_thread_b_takeover)
    thread_b.start()
    thread_b.join(timeout=2)
    assert thread_b.is_alive(), "execute_takeover() did NOT block on the goal-row lock thread A holds -- the fence is not real"

    release_lock.set()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    assert results.get("envelope_id") is not None  # thread A's first envelope committed
    assert results.get("new_job_id") is None  # thread B correctly observed EVER_GOVERNED and declined -- never minted a V0.1 job

    dead_row = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :id"), {"id": str(dead_job_id)}).one()
    assert dead_row[0] == "failed"


def test_first_governance_toctou_race_recovery_committed_first_then_governance_follows(
    db_session, superuser_db, owner_id
):
    """Ordering A (acceptable): legacy recovery obtains the serialization boundary first ->
    completes its legacy transition -> only then can first governance become effective. Thread
    A holds the goal-row lock and, while still holding it, runs the REAL `execute_takeover()`
    for this never-governed goal (a genuine V0.1 dispatch). Thread B's real
    `authorize_execution_scope()` call must block until thread A commits. FORBIDDEN outcome
    this proves does NOT happen: the envelope becoming active WHILE thread A's decision was
    still in flight (it cannot, since thread B cannot even start writing until thread A's
    entire transaction -- decision AND action -- is durably committed)."""
    import asyncio
    import threading

    from app.models.mainai_execution import MainAIGoal

    goal = _goal(db_session, owner_id)
    db_session.commit()

    task, dead_job = _task_and_dead_job(db_session, superuser_db, owner_id, goal)
    record = asyncio.run(_through_classification(db_session, task, dead_job))
    record_id, task_id, goal_id = record.id, task.id, goal.id
    db_session.commit()

    lock_held = threading.Event()
    release_lock = threading.Event()
    results: dict = {}

    def _thread_a_hold_then_takeover():
        session = _make_session()
        try:
            session.execute(sa_text("SELECT id FROM mainai_goals WHERE id = :id FOR UPDATE"), {"id": str(goal_id)})
            lock_held.set()
            release_lock.wait(timeout=10)
            goal_row = session.get(MainAIGoal, goal_id)
            task_row = session.get(MainAITask, task_id)
            record_row = session.get(MainAIRecoveryRecord, record_id)

            async def _run():
                return await execute_takeover(session, task=task_row, goal=goal_row, record=record_row, dispatched_by="toctou-recovery")

            _record_out, new_job = asyncio.run(_run())
            session.commit()
            results["new_job_id"] = new_job.id if new_job is not None else None
        finally:
            session.close()

    def _thread_b_authorize():
        session = _make_session()
        try:
            proposal = propose_execution_scope(session, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"toctou-a-prop-{uuid.uuid4()}")
            _, envelope = authorize_execution_scope(
                session, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                authorized_paths=["README.md"], authorized_capabilities=["read_file"], authorized_risk="low",
                envelope_idempotency_key=f"toctou-a-env-{uuid.uuid4()}",
            )
            session.commit()
            results["envelope_id"] = envelope.id
        finally:
            session.close()

    thread_a = threading.Thread(target=_thread_a_hold_then_takeover)
    thread_a.start()
    assert lock_held.wait(timeout=10), "thread A never acquired the goal-row lock"

    thread_b = threading.Thread(target=_thread_b_authorize)
    thread_b.start()
    thread_b.join(timeout=2)
    assert thread_b.is_alive(), "authorize_execution_scope() did NOT block on the goal-row lock thread A holds -- the fence is not real"

    release_lock.set()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    assert results.get("new_job_id") is not None  # legacy takeover dispatched -- goal was never governed at decision time
    assert results.get("envelope_id") is not None  # founder's authorization only completes AFTER, never concurrently
