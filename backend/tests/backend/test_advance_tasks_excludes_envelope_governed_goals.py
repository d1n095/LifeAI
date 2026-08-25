"""app/worker.py's `_advance_mainai_execution_tasks()` (MainAI Execution Loop V0.1's own
blanket, approval-policy-only auto-dispatch tick) must EXCLUDE any task whose goal has EVER
had an ExecutionAuthorizationEnvelope row (any status, not only active) -- otherwise the whole
envelope/Supervisor foundation this session built would be decorative: V0.1's tick already
unconditionally dispatches every `ready` `standard_repo_work`-policy task (repo_edit/
run_tests/open_pr/read_only_audit are ALL marked AUTO, requiring no approval, in that policy
-- see app/mainai_execution/approval.py's APPROVAL_POLICIES) with ZERO awareness of any path/
capability/risk envelope at all, and would otherwise win the race against
app.development_supervisor.production_entry's bounded, envelope-scoped path simply by running
first in the same poll cycle (see app/worker.py's own run_once() ordering).

REVOCATION != FALLBACK TO OLDER AUTHORITY (founder decision, applied here after Cursor's own
adversarial PR #152 pinned the original gap): once a goal enters envelope governance, an
absent/superseded/revoked envelope must fail closed for autonomous execution -- it must never
implicitly reopen this wider, envelope-blind path. See app/worker.py's own
`_advance_mainai_execution_tasks()` docstring for the exact mechanism (existence of ANY
`execution_authorization_envelopes` row for a goal_id, not just an active one, is itself the
durable "ever governed" fact)."""

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
async def test_superseding_the_only_envelope_without_a_replacement_fails_closed_never_reopens_v01(
    superuser_db, make_verified_user,
):
    """The founder decision this test now enforces (was a LATENT gap, pinned by Cursor's own
    adversarial PR #152, then closed here): once a goal has ever been envelope-governed,
    revoking its only active envelope with no replacement must NOT reopen V0.1's wider,
    envelope-blind auto-dispatch. `AUTHORITY REVOKED != FALLBACK TO OLDER, LESS-SCOPED
    AUTHORITY` -- the goal must simply become undispatchable by either path (fail closed) until
    a founder explicitly re-authorizes it."""
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
    assert task.status == MainAITaskStatus.ready, "must fail closed, never reopen V0.1's wider auto-dispatch"
    assert task.mainai_job_id is None


@pytest.mark.asyncio
async def test_reauthorizing_after_supersession_allows_the_supervisor_path_only_never_v01(superuser_db, make_verified_user):
    """The complementary case: a goal that WAS revoked (no active envelope) and is then
    RE-authorized must go back under Supervisor-only governance -- never V0.1, even though it
    now has a genuinely active envelope again. Confirms the exclusion is keyed on "ever had a
    row", not merely "currently has an active one", for goals with a real supersession
    history."""
    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope

    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    _authorize(superuser_db, owner.id, goal.id)
    superuser_db.commit()

    proposal = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="excl-test-prop-2")
    authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["README.md"], authorized_capabilities=["read_file"], authorized_risk="low",
        envelope_idempotency_key="excl-test-env-2",
    )
    superuser_db.commit()

    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()

    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    assert task.status == MainAITaskStatus.ready  # still excluded from V0.1 -- Supervisor-only
    assert task.mainai_job_id is None


@pytest.mark.asyncio
async def test_a_never_governed_ordinary_goal_is_unaffected_by_the_ever_governed_exclusion(superuser_db, make_verified_user):
    """A goal that has NEVER had any execution_authorization_envelopes row at all -- the
    ordinary, non-autonomous, founder-created-goal case -- must be completely unaffected by
    this exclusion. Distinct from
    test_a_ready_task_under_an_unauthorized_goal_is_still_auto_dispatched_as_before above only
    in naming to make the "never governed" invariant explicit."""
    owner, _ = make_verified_user()
    goal = _goal_with_ready_task(superuser_db, owner.id)
    superuser_db.commit()

    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()

    task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    assert task.status == MainAITaskStatus.running
    assert task.mainai_job_id is not None


@pytest.mark.asyncio
async def test_a_goal_authorized_mid_batch_still_blocks_its_own_later_task_in_the_same_tick(superuser_db, make_verified_user, monkeypatch):
    """Closes the TOCTOU window explicitly: two DIFFERENT, never-governed goals both have a
    `ready` task at the moment the tick's own initial batch SELECT runs (neither excluded
    yet). While processing whichever task the tick reaches FIRST, the OTHER goal becomes
    envelope-governed (a real founder action landing mid-tick, simulated here as a side
    effect). The per-task re-check immediately before dispatch -- not just the one-time batch
    filter computed before either task ran -- must still catch it for the second-processed
    goal's task, proving the re-check is not redundant with the batch query. Order-independent
    on purpose: `_advance_mainai_execution_tasks`'s own query has no explicit ORDER BY, so
    which goal is processed first is not something this test may assume."""
    import app.worker as worker_module

    owner, _ = make_verified_user()
    goal_a = _goal_with_ready_task(superuser_db, owner.id)
    goal_b = _goal_with_ready_task(superuser_db, owner.id)
    superuser_db.commit()

    original_dispatch = worker_module.dispatch_ready_task
    calls = []

    def _dispatch_then_authorize_the_other_goal(db, *, task, goal, **kwargs):
        calls.append(goal.id)
        result = original_dispatch(db, task=task, goal=goal, **kwargs)
        if len(calls) == 1:  # first dispatch this tick -- authorize whichever goal hasn't run yet
            other_goal_id = goal_b.id if goal.id == goal_a.id else goal_a.id
            _authorize(db, owner.id, other_goal_id)
            db.commit()
        return result

    monkeypatch.setattr(worker_module, "dispatch_ready_task", _dispatch_then_authorize_the_other_goal)

    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()

    assert len(calls) == 1  # only the first-processed goal's task actually dispatched
    blocked_goal_id = goal_b.id if calls[0] == goal_a.id else goal_a.id
    blocked_task = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == blocked_goal_id)).scalar_one()
    assert blocked_task.status == MainAITaskStatus.ready  # the per-task re-check caught the mid-tick change
    assert blocked_task.mainai_job_id is None
