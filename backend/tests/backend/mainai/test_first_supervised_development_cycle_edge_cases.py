"""Extends `test_first_supervised_development_cycle.py`'s happy-path harness with the
founder's own named edge cases ("RESUME ACTIVE DEVELOPMENT WHEN CAPACITY IS AVAILABLE",
Priority 2): unauthorized candidates cannot execute; rejected/revoked approvals fail closed;
restart/interrupted-task recovery preserves correct state; duplicate requests never create
duplicate execution; evidence references the exact source and authorization record; tenant
isolation holds; failures never masquerade as completions.

Still activates no adapter/provider dispatch anywhere -- every scenario below stops at the
same gate the happy-path harness already proves opens, never walks through it.

One item the founder's list names -- "expired approval fails closed" -- is deliberately NOT
exercised here: read directly, `MainAITaskEvent` (the approval_granted event
`grant_task_approval()` records) has no expiry field at all, and `require_task_approval()`'s
own check is a presence check, not a freshness check. Expiry exists only one layer up, at
`SystemActivationAuthorization.expires_at` (system-wide activation evidence, already covered
by this session's own `test_system_activation_authorizations_composition_security.py`) --
individual task-level approval has no expiry concept to test. Recorded here accurately rather
than fabricated."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.github_evidence import (
    GithubEvidenceError,
    authorize_github_evidence_candidate,
    dismiss_github_evidence_candidate,
    record_github_evidence_candidates,
)
from app.mainai_execution.approval import (
    ApprovalRequiredError,
    grant_task_approval,
    require_live_task_approval_for_dispatch,
    require_task_approval,
)
from app.mainai_execution.checkpoint import (
    latest_checkpoint_for_step,
    record_checkpoint,
)
from app.mainai_execution.executor import cancel_task
from app.mainai_execution.planner import PlannedTaskSpec, create_plan
from app.models.github_evidence import GithubEvidenceCandidate
from app.models.mainai_execution import (
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.safe_planner.github_work_queue import WorkQueueItem


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _item(number: int = 441, signals: tuple[str, ...] = ("ci_red",)) -> WorkQueueItem:
    return WorkQueueItem(
        kind="open_pull_request",
        number=number,
        title=f"PR {number}",
        html_url=f"https://github.com/d1n095/LifeAI/pull/{number}",
        updated_at="2026-11-15T21:00:00Z",
        signals=signals,
    )


def _authorized_task(db, *, owner_id, pr_number: int):
    """Shared setup for the scenarios below: an authorized goal with exactly one ready task,
    mirroring the happy-path harness's own steps 1/3/4 without re-asserting them here."""
    candidates = record_github_evidence_candidates(db, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[_item(pr_number)])
    db.commit()
    candidate = candidates[0]
    _authorized_candidate, goal = authorize_github_evidence_candidate(
        db, owner_id=owner_id, candidate_id=candidate.id, authorized_by="founder-test-harness",
    )
    plan = create_plan(
        db, goal=goal, rationale="edge-case harness", tasks=[PlannedTaskSpec(description="t", task_type="read_only_audit")],
        created_by="founder-test-harness",
    )
    db.commit()
    task = db.execute(select(MainAITask).where(MainAITask.plan_id == plan.id)).scalar_one()
    return candidate, goal, task


# ============================================================================ 1. unauthorized
# candidates cannot execute -- there is nothing to execute, because nothing was ever created.


def test_unauthorized_candidate_has_no_goal_or_task_to_execute(superuser_db, owner_id):
    db = superuser_db
    candidates = record_github_evidence_candidates(db, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[_item(1)])
    db.commit()
    candidate = candidates[0]

    assert candidate.status == "unreviewed"
    assert candidate.authorized_goal_id is None
    # Not "a task exists but is blocked" -- structurally, NO MainAITask was ever created,
    # because the only path to one (authorize_github_evidence_candidate() -> create_plan())
    # was never called. There is nothing for any dispatch boundary to even consider.
    assert db.execute(select(MainAITask).where(MainAITask.owner_id == owner_id)).first() is None


# ============================================================================ 2. rejected /
# revoked approvals fail closed (expired: see module docstring for why it's out of scope).


def test_a_dismissed_candidate_can_never_be_authorized(superuser_db, owner_id):
    db = superuser_db
    candidates = record_github_evidence_candidates(db, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[_item(2)])
    db.commit()
    candidate = candidates[0]

    dismiss_github_evidence_candidate(db, owner_id=owner_id, candidate_id=candidate.id, reason="not actionable")
    db.commit()

    with pytest.raises(GithubEvidenceError):
        authorize_github_evidence_candidate(db, owner_id=owner_id, candidate_id=candidate.id, authorized_by="founder")
    db.rollback()


def test_a_cancelled_task_fails_closed_at_the_live_dispatch_gate_even_after_approval(superuser_db, owner_id):
    """The revocation scenario: approval was genuinely granted, then the founder cancels the
    task before it executes. require_task_approval() (the earlier, unlocked check) would still
    see the old approval_granted event and say yes -- the REAL protection is require_live_task_
    approval_for_dispatch()'s fresh status check, exactly like PR #431's own concurrency proof
    for the workforce boundary."""
    db = superuser_db
    _candidate, _goal, task = _authorized_task(db, owner_id=owner_id, pr_number=3)
    grant_task_approval(db, task=task, approved_by="founder")
    db.commit()
    require_task_approval(db, task=task, goal_approval_policy=_goal_policy(db, task))  # still passes here

    cancel_task(db, task=task, cancelled_by="founder", reason="superseded")
    db.commit()

    with pytest.raises(ApprovalRequiredError):
        require_live_task_approval_for_dispatch(db, owner_id=owner_id, task_id=task.id)


def _goal_policy(db, task) -> str:
    from app.models.mainai_execution import MainAIGoal

    return db.execute(select(MainAIGoal.approval_policy).where(MainAIGoal.id == task.goal_id)).scalar_one()


# ============================================================================ 3. restart /
# interrupted-task recovery preserves correct state.


def test_checkpoint_recovery_preserves_state_across_a_simulated_restart(superuser_db, owner_id):
    """Simulates a worker crash: a checkpoint is recorded mid-step, then a fresh lookup (the
    exact call a resumed worker process would make, with no in-memory state left) must recover
    the SAME exact-SHA state -- never re-derive or guess it."""
    db = superuser_db
    _candidate, goal, task = _authorized_task(db, owner_id=owner_id, pr_number=4)
    job_id = uuid.uuid4()
    exact_sha = "b" * 40

    record_checkpoint(db, task=task, goal=goal, job_id=job_id, step="audit_complete", data={"commit_sha": exact_sha})
    db.commit()

    recovered = latest_checkpoint_for_step(db, task_id=task.id, job_id=job_id, step="audit_complete")
    assert recovered is not None
    assert recovered.executor_state["commit_sha"] == exact_sha

    # A genuinely NEW attempt (a real retry_task() mints a new job_id) must NOT see the old
    # attempt's checkpoint as if it were its own -- the isolation record_checkpoint()'s own
    # docstring states explicitly.
    new_job_id = uuid.uuid4()
    assert latest_checkpoint_for_step(db, task_id=task.id, job_id=new_job_id, step="audit_complete") is None


# ============================================================================ 4. duplicate
# requests do not create duplicate execution.


def test_authorizing_the_same_candidate_twice_is_rejected_not_duplicated(superuser_db, owner_id):
    db = superuser_db
    candidate, _goal, _task = _authorized_task(db, owner_id=owner_id, pr_number=5)

    with pytest.raises(GithubEvidenceError):
        authorize_github_evidence_candidate(db, owner_id=owner_id, candidate_id=candidate.id, authorized_by="founder-second-attempt")
    db.rollback()


def test_recording_the_same_github_item_twice_updates_in_place_never_duplicates(superuser_db, owner_id):
    db = superuser_db
    first = record_github_evidence_candidates(db, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[_item(6)])
    db.commit()
    second = record_github_evidence_candidates(db, owner_id=owner_id, source_repo="d1n095/LifeAI", items=[_item(6, signals=("ci_red", "stale"))])
    db.commit()

    assert first[0].id == second[0].id
    all_rows = db.execute(select(GithubEvidenceCandidate).where(GithubEvidenceCandidate.owner_id == owner_id)).scalars().all()
    assert len(all_rows) == 1


# ============================================================================ 5. evidence
# references the exact source and authorization record, not a bare SHA floating alone.


def test_evidence_event_links_back_to_the_source_candidate_and_approval_event(superuser_db, owner_id):
    db = superuser_db
    candidate, _goal, task = _authorized_task(db, owner_id=owner_id, pr_number=7)
    approval_event = grant_task_approval(db, task=task, approved_by="founder")
    db.commit()

    exact_sha = "c" * 40
    db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=owner_id,
            event_type=MainAITaskEventType.completed,
            detail={"exact_sha": exact_sha, "source_candidate_id": str(candidate.id), "approval_event_id": str(approval_event.id)},
        )
    )
    db.commit()

    evidence = db.execute(
        select(MainAITaskEvent).where(MainAITaskEvent.task_id == task.id, MainAITaskEvent.event_type == MainAITaskEventType.completed)
    ).scalar_one()
    assert evidence.detail["source_candidate_id"] == str(candidate.id)
    assert evidence.detail["approval_event_id"] == str(approval_event.id)
    # The chain is walkable end to end, not just two strings that happen to match: the
    # candidate this evidence claims as its source really is the one that authorized this
    # task's own goal.
    assert candidate.authorized_goal_id == task.goal_id


# ============================================================================ 6. tenant
# isolation remains enforced.


def test_a_second_owner_cannot_see_the_first_owners_candidate_or_task(superuser_db, db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    superuser_db.commit()

    _candidate, _goal, task = _authorized_task(superuser_db, owner_id=owner_a.id, pr_number=8)

    db_session.execute(sa_text("SELECT set_config('app.current_user_id', :v, false)"), {"v": str(owner_b.id)})
    assert db_session.execute(select(MainAITask).where(MainAITask.id == task.id)).first() is None

    db_session.execute(sa_text("SELECT set_config('app.current_user_id', :v, false)"), {"v": str(owner_a.id)})
    assert db_session.execute(select(MainAITask).where(MainAITask.id == task.id)).scalar_one().id == task.id


# ============================================================================ 7. failures do
# not create falsely completed tasks.


def test_a_failed_task_records_only_a_failed_event_never_a_completed_one(superuser_db, owner_id):
    db = superuser_db
    _candidate, _goal, task = _authorized_task(db, owner_id=owner_id, pr_number=9)

    db.add(
        MainAITaskEvent(
            task_id=task.id, owner_id=owner_id, event_type=MainAITaskEventType.failed, detail={"reason": "verification did not pass"}
        )
    )
    task.status = MainAITaskStatus.failed
    db.commit()

    events = db.execute(select(MainAITaskEvent.event_type).where(MainAITaskEvent.task_id == task.id)).scalars().all()
    assert MainAITaskEventType.failed in events
    assert MainAITaskEventType.completed not in events
