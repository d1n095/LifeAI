"""app/worker.py's `_advance_mainai_execution_tasks()` (MainAI Execution Loop V0.1's own
blanket, approval-policy-only auto-dispatch tick) must EXCLUDE any task whose goal currently
has an active ExecutionAuthorizationEnvelope -- otherwise the whole envelope/Supervisor
foundation this session built would be decorative: V0.1's tick already unconditionally
dispatches every `ready` `standard_repo_work`-policy task (repo_edit/run_tests/open_pr/
read_only_audit are ALL marked AUTO, requiring no approval, in that policy -- see
app/mainai_execution/approval.py's APPROVAL_POLICIES) with ZERO awareness of any path/
capability/risk envelope at all, and would otherwise win the race against
app.development_supervisor.production_entry's bounded, envelope-scoped path simply by running
first in the same poll cycle (see app/worker.py's own run_once() ordering)."""

import pytest
from sqlalchemy import select

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.worker import Worker


def _goal_with_ready_task(db, owner_id):
    goal = planner.create_goal(db, owner_id=owner_id, title="exclusion test", original_instruction="edit a file", created_by="test")
    planner.create_plan(
        db, goal=goal, rationale="single task",
        tasks=[PlannedTaskSpec(description="edit a file", task_type="repo_edit")],
        created_by="test",
    )
    db.flush()
    return goal


def _authorize(db, owner_id, goal_id):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal_id, idempotency_key="excl-test-prop")
    _, envelope = authorize_execution_scope(
        db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["README.md"], authorized_capabilities=["read_file"], authorized_risk="low",
        envelope_idempotency_key="excl-test-env",
    )
    return envelope


@pytest.mark.asyncio
async def test_a_ready_task_under_an_envelope_governed_goal_is_never_auto_dispatched_by_v01(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    task = superuser_db.execute(
        select(MainAITask).where(MainAITask.goal_id == goal.id)
    ).scalar_one()
    assert task.status == MainAITaskStatus.ready

    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.ready  # untouched -- reserved for the Supervisor path
    assert task.mainai_job_id is None


@pytest.mark.asyncio
async def test_a_ready_task_under_an_unauthorized_goal_is_still_auto_dispatched_as_before(superuser_db, make_verified_user):
    """The exclusion is narrow: a goal with no envelope at all is completely unaffected --
    V0.1's existing behavior for the normal, non-autonomous, founder-created-goal case must
    not regress."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    superuser_db.commit()

    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()

    task = superuser_db.execute(
        select(MainAITask).where(MainAITask.goal_id == goal.id)
    ).scalar_one()
    assert task.status == MainAITaskStatus.running
    assert task.mainai_job_id is not None


@pytest.mark.asyncio
async def test_superseding_an_envelope_without_replacement_currently_reopens_v01_auto_dispatch(
    superuser_db, make_verified_user,
):
    """LATENT Class-B characterization, not a pass/fail of desired product behavior.

    `_advance_mainai_execution_tasks` excludes goals with an *active* envelope only. There is
    today no founder API that supersedes an envelope WITHOUT authorizing a replacement
    (`authorize_execution_scope` always leaves a new active row), so this state is not on a
    production path yet. If/when a revoke-without-replacement surface is added, this test
    pins the CURRENT composition: Supervisor becomes ineligible (correct) AND V0.1's
    envelope-blind auto-dispatch resumes (possibly NOT what a founder who just revoked
    autonomous authority intended).

    `AUTHORITY REVOKED != ALL AUTONOMOUS DISPATCH STOPPED` until that decision is made
    explicitly. Do not "fix" this by widening the exclusion without a founder call -- the
    safe default for a future revoke API is more likely "stay off V0.1 too", which would
    deliberately flip this assertion."""
    from app.models.execution_envelope import ExecutionAuthorizationEnvelope

    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    assert task.status == MainAITaskStatus.ready

    # Simulate a future revoke-without-replacement (no production API today).
    superuser_db.query(ExecutionAuthorizationEnvelope).filter(
        ExecutionAuthorizationEnvelope.goal_id == goal.id
    ).update({"status": "superseded"})
    superuser_db.commit()

    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()

    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.running, (
        "CURRENT behavior: superseding the only envelope without a replacement re-opens V0.1 "
        "auto-dispatch. If a revoke API is added, decide whether this is still desired."
    )
    assert task.mainai_job_id is not None
