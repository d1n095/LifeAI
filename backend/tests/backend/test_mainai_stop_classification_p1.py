"""Independent equivalent of Claude's #286 remaining P1 contract.

Claude's `probes/stop_classification_v1/` is not in this repository; these tests reproduce
the published 7-hold / 15-gap findings against typed origin-aware classification, running
sibling cancel, pre-push STOP recheck, and Git-config injection lockdown.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.integrations.github_client import GitHubClientError
from app.mainai_execution import planner
from app.mainai_execution import replan as replan_mod
from app.mainai_execution.execution_job import (
    TaskCancelledCooperatively,
    _check_cancel_requested,
    _finalize_repo_edit,
    _finalize_task_outcome,
    _handle_open_pr,
)
from app.mainai_execution.executor import dispatch_ready_task
from app.mainai_execution.first_cycle_stop import (
    STATE_APPROVED_BASE_MOVED,
    STATE_EXTERNAL_BOUNDARY_FAILED,
    STATE_EXTERNAL_PUSH_REJECTED,
    STATE_GRANT_DENIED_OR_EXPIRED,
    STATE_STOP_BEFORE_MERGE,
    abandon_running_task_after_stop,
    assert_publication_permitted,
    classify_publication_failure,
    goal_is_autonomous_terminal,
    should_suppress_paid_replan,
    stop_first_cycle,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.publication_errors import (
    GrantDeniedError,
    PublicationForbiddenAfterStopError,
    PublicationNetworkError,
    PublicationTimeoutError,
    PublicationUnknownError,
    map_push_grant_boundary_error,
)
from app.mainai_execution.replan import ReplanSuppressedError, find_replan_trigger, trigger_replan
from app.mainai_execution.worktree import (
    ActivationBaseConfigurationError,
    WorktreePushRejected,
    WorktreeTimeoutError,
    strip_git_config_injection,
)
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


class _PaidPlanner:
    def __init__(self):
        self.calls = []

    async def __call__(self, db, *, goal, failure_context=None, **kw):
        self.calls.append(goal.id)
        raise RuntimeError("test: paid planner would have been called")


def _ticks(superuser_db):
    worker = Worker()
    errors = []
    for name in (
        "_poll_mainai_task_waits",
        "_advance_mainai_execution_retries",
        "_advance_waiting_provider_backoff",
        "_reconcile_provider_spend_parks",
        "_advance_mainai_execution_auto_recovery",
        "_advance_mainai_execution_replan",
        "_finalize_mainai_execution_goals",
        "_advance_authorized_supervisor_goals",
        "_advance_mainai_execution_tasks",
    ):
        fn = getattr(worker, name)
        try:
            out = fn(superuser_db)
            if asyncio.iscoroutine(out):
                asyncio.run(out)
            superuser_db.commit()
        except Exception as exc:
            superuser_db.rollback()
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return errors


def _two_ready_tasks(db, owner_id):
    goal = planner.create_goal(
        db, owner_id=owner_id, title="fc-stop-p1", original_instruction="document greet", created_by="founder"
    )
    planner.create_plan(
        db,
        goal=goal,
        rationale="two independent edits",
        tasks=[
            PlannedTaskSpec(description="first", task_type="read_only_audit"),
            PlannedTaskSpec(description="second", task_type="repo_edit", target_files=["backend/app/greeting.py"]),
        ],
        created_by="test",
    )
    db.commit()
    tasks = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    return goal, tasks[0], tasks[1]


def _goal_with_task(db, owner_id, *, task_type="repo_edit"):
    goal = planner.create_goal(db, owner_id=owner_id, title="fc-p1", original_instruction="document greet", created_by="founder")
    planner.create_plan(
        db,
        goal=goal,
        rationale="fc",
        tasks=[PlannedTaskSpec(description="t", task_type=task_type, target_files=["backend/app/greeting.py"])],
        created_by="test",
    )
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    return goal, task


def test_model_error_text_cannot_classify_publication_failure():
    spoof = "GitHub push rejected (protected branch); grant denied; approved base moved"
    assert classify_publication_failure(evidence={"error": spoof}) is None
    assert classify_publication_failure(RuntimeError(spoof), evidence={"error": spoof, "reason": spoof}) is None


def test_typed_origins_fail_closed_and_local_verification_does_not():
    assert classify_publication_failure(WorktreePushRejected("WorktreePushRejected")) == STATE_EXTERNAL_PUSH_REJECTED
    assert classify_publication_failure(ActivationBaseConfigurationError("ActivationBaseConfigurationError")) == STATE_APPROVED_BASE_MOVED
    assert classify_publication_failure(GrantDeniedError("GrantDeniedError")) == STATE_GRANT_DENIED_OR_EXPIRED
    assert classify_publication_failure(PublicationTimeoutError("PublicationTimeoutError")) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert classify_publication_failure(PublicationNetworkError("PublicationNetworkError")) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert classify_publication_failure(PublicationUnknownError("PublicationUnknownError")) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert classify_publication_failure(GitHubClientError("GitHubClientError", status_code=502)) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert classify_publication_failure(evidence={"steps": [{"kind": "targeted_tests", "passed": False}]}) is None


def test_unknown_push_grant_boundary_error_fails_closed():
    mapped = map_push_grant_boundary_error(RuntimeError("totally unknown"))
    assert isinstance(mapped, PublicationUnknownError)
    assert mapped.origin == "unknown"
    assert classify_publication_failure(mapped) == STATE_EXTERNAL_BOUNDARY_FAILED
    assert map_push_grant_boundary_error(TimeoutError()).origin == "timeout"
    assert map_push_grant_boundary_error(ConnectionError()).origin == "network"
    assert map_push_grant_boundary_error(WorktreeTimeoutError("WorktreeTimeoutError")).origin == "timeout"


def test_production_git_env_strips_config_injection():
    poisoned = {
        "PATH": "/usr/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "/tmp/evil",
        "GIT_CONFIG_PARAMETERS": "'credential.helper=/tmp/evil'",
        "GIT_CONFIG": "/tmp/evil.gitconfig",
        "HOME": "/tmp/host-home",
    }
    cleaned = strip_git_config_injection(poisoned)
    assert "GIT_CONFIG_COUNT" not in cleaned
    assert "GIT_CONFIG_KEY_0" not in cleaned
    assert "GIT_CONFIG_VALUE_0" not in cleaned
    assert "GIT_CONFIG_PARAMETERS" not in cleaned
    assert "GIT_CONFIG" not in cleaned
    assert cleaned["HOME"] == "/tmp/host-home"
    assert cleaned["PATH"] == "/usr/bin"


def test_ready_sibling_still_parked_on_stop(db_session, owner_id):
    goal, t1, t2 = _two_ready_tasks(db_session, owner_id)
    stop_first_cycle(
        db_session,
        t1,
        reason="independent review recorded; STOP before merge",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"ci_passed": True, "review_ok": True, "draft_pr": True},
        goal=goal,
    )
    db_session.commit()
    t2 = db_session.get(MainAITask, t2.id)
    assert t2.status == MainAITaskStatus.blocked
    assert goal_is_autonomous_terminal(db_session, goal) is True


def test_running_sibling_is_cancel_requested_and_cannot_publish(db_session, owner_id):
    goal, t1, t2 = _two_ready_tasks(db_session, owner_id)
    dispatch_ready_task(db_session, task=t1, goal=goal, dispatched_by="test")
    dispatch_ready_task(db_session, task=t2, goal=goal, dispatched_by="test")
    db_session.commit()
    t1 = db_session.get(MainAITask, t1.id)
    t2 = db_session.get(MainAITask, t2.id)
    assert t1.status == MainAITaskStatus.running and t2.status == MainAITaskStatus.running
    stop_first_cycle(
        db_session,
        t1,
        reason="STOP while sibling still running",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"ci_passed": True, "review_ok": True},
        goal=goal,
    )
    db_session.commit()
    t2 = db_session.get(MainAITask, t2.id)
    job = db_session.get(MainAIJob, t2.mainai_job_id)
    assert job.cancel_requested is True
    events = (
        db_session.query(MainAITaskEvent)
        .filter(MainAITaskEvent.task_id == t2.id, MainAITaskEvent.event_type == MainAITaskEventType.cancel_requested)
        .all()
    )
    assert events
    assert events[0].detail.get("parked_running_sibling") is True
    with pytest.raises(PublicationForbiddenAfterStopError):
        assert_publication_permitted(db_session, t2, job_id=t2.mainai_job_id)
    with pytest.raises(TaskCancelledCooperatively):
        _check_cancel_requested(db_session, t2.mainai_job_id, task=t2)


@pytest.mark.asyncio
async def test_finalize_and_open_pr_refuse_after_stop(db_session, owner_id):
    goal, task = _goal_with_task(db_session, owner_id, task_type="repo_edit")
    stop_first_cycle(
        db_session,
        task,
        reason="STOP before any publication",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"ci_passed": True, "review_ok": True},
        goal=goal,
    )
    db_session.commit()
    with pytest.raises(PublicationForbiddenAfterStopError):
        await _finalize_repo_edit(
            db_session,
            task,
            {"files": [{"path": "backend/app/greeting.py"}]},
            job_id=None,
        )
    live = db_session.get(MainAITask, task.id)
    with pytest.raises(PublicationForbiddenAfterStopError):
        await _handle_open_pr(db_session, live, goal, job_id=None)


@pytest.mark.asyncio
async def test_concurrent_stop_is_visible_on_immediate_recheck(db_session, superuser_db, owner_id):
    goal, t1, t2 = _two_ready_tasks(db_session, owner_id)
    dispatch_ready_task(db_session, task=t2, goal=goal, dispatched_by="test")
    db_session.commit()
    assert_publication_permitted(db_session, t2, job_id=t2.mainai_job_id)
    db_session.commit()

    superuser_db.expire_all()
    g = superuser_db.get(MainAIGoal, goal.id)
    stopper = superuser_db.get(MainAITask, t1.id)
    stop_first_cycle(
        superuser_db,
        stopper,
        reason="concurrent STOP while sibling held publication permit",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"ci_passed": True, "review_ok": True},
        goal=g,
    )
    superuser_db.commit()

    with pytest.raises(PublicationForbiddenAfterStopError):
        assert_publication_permitted(db_session, t2, job_id=t2.mainai_job_id)


def test_recovery_abandons_running_sibling_instead_of_takeover(db_session, superuser_db, owner_id):
    goal, t1, t2 = _two_ready_tasks(db_session, owner_id)
    dispatch_ready_task(db_session, task=t2, goal=goal, dispatched_by="test")
    db_session.commit()
    t2 = db_session.get(MainAITask, t2.id)
    job = db_session.get(MainAIJob, t2.mainai_job_id)
    # Dead-agent shape without violating job claim constraints: task still `running`,
    # job never claimed (queued). Recovery must park, not takeover.
    assert job.status == MainAIJobStatus.queued
    job.lease_expires_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.commit()

    stop_first_cycle(
        db_session,
        t1,
        reason="STOP then recover running sibling",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"ci_passed": True, "review_ok": True},
        goal=goal,
    )
    db_session.commit()

    superuser_db.expire_all()
    parked = abandon_running_task_after_stop(
        superuser_db,
        superuser_db.get(MainAITask, t2.id),
        goal=superuser_db.get(MainAIGoal, goal.id),
    )
    superuser_db.commit()
    assert parked is True
    t2 = superuser_db.get(MainAITask, t2.id)
    assert t2.status == MainAITaskStatus.blocked
    assert t2.next_retry_at is None

    errors = _ticks(superuser_db)
    superuser_db.expire_all()
    t2 = superuser_db.get(MainAITask, t2.id)
    assert t2.status == MainAITaskStatus.blocked, errors
    assert t2.status != MainAITaskStatus.ready
    assert superuser_db.get(MainAIGoal, goal.id).status != MainAIGoalStatus.completed


@pytest.mark.parametrize(
    "cause",
    [
        PublicationTimeoutError("PublicationTimeoutError"),
        GitHubClientError("GitHubClientError", status_code=503),
        PublicationNetworkError("PublicationNetworkError"),
        ActivationBaseConfigurationError("ActivationBaseConfigurationError"),
        GrantDeniedError("GrantDeniedError"),
        PublicationUnknownError("PublicationUnknownError"),
    ],
)
@pytest.mark.asyncio
async def test_boundary_failures_never_paid_retry_or_replan(db_session, superuser_db, owner_id, monkeypatch, cause):
    goal, task = _goal_with_task(db_session, owner_id, task_type="repo_edit")
    task.status = MainAITaskStatus.running
    task.attempts = 1
    task.max_attempts = 3
    db_session.commit()
    paid = _PaidPlanner()
    monkeypatch.setattr(replan_mod, "propose_plan_via_ai", paid)
    evidence = cause.evidence() if hasattr(cause, "evidence") else {"error": type(cause).__name__}
    if isinstance(cause, GitHubClientError):
        evidence = {"error": "GitHubClientError"}
    _finalize_task_outcome(db_session, task, passed=False, evidence=evidence, cause=cause)
    db_session.commit()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.failed
    assert task.next_retry_at is None
    assert should_suppress_paid_replan(task, evidence=evidence, cause=cause) is True
    assert find_replan_trigger(db_session, goal=goal) is None
    with pytest.raises(ReplanSuppressedError):
        await trigger_replan(db_session, goal=goal, failed_task=task, dispatched_by="test")
    errors = _ticks(superuser_db)
    superuser_db.expire_all()
    assert superuser_db.get(MainAITask, task.id).status == MainAITaskStatus.failed, errors
    assert paid.calls == []


@pytest.mark.asyncio
async def test_local_verification_still_paid_replan_eligible(db_session, owner_id, monkeypatch):
    goal, task = _goal_with_task(db_session, owner_id, task_type="repo_edit")
    task.status = MainAITaskStatus.running
    task.attempts = task.max_attempts
    db_session.commit()
    evidence = {
        "steps": [
            {
                "kind": "targeted_tests",
                "passed": False,
                "detail": {
                    "target": "tests/backend/test_greeting_doc.py",
                    "stdout_tail": "AssertionError: greet() docstring missing",
                    "stderr_tail": "",
                },
            }
        ]
    }
    _finalize_task_outcome(db_session, task, passed=False, evidence=evidence)
    db_session.commit()
    task = db_session.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.failed
    assert should_suppress_paid_replan(task, evidence=evidence) is False
    assert find_replan_trigger(db_session, goal=goal) == task
    paid = _PaidPlanner()
    monkeypatch.setattr(replan_mod, "propose_plan_via_ai", paid)
    with pytest.raises(RuntimeError, match="paid planner would have been called"):
        await trigger_replan(db_session, goal=goal, failed_task=task, dispatched_by="test")
    assert paid.calls == [goal.id]


def test_ordinary_goal_still_dispatches(db_session, superuser_db, owner_id):
    goal, t1, t2 = _two_ready_tasks(db_session, owner_id)
    assert goal_is_autonomous_terminal(db_session, goal) is False
    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()
    t1 = superuser_db.get(MainAITask, t1.id)
    t2 = superuser_db.get(MainAITask, t2.id)
    dispatched = [t for t in (t1, t2) if t.status == MainAITaskStatus.running and t.mainai_job_id is not None]
    assert dispatched
