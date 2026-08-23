"""MainAI V0.3 -- final_report.py extension for the five new long-running-orchestration
signals: external waits, automatic retry scheduling, cooperative cancellation, automatic
replan, and engineering-lesson conflicts. Same discipline as the rest of final_report.py: pure
aggregation over already-durable rows, never an LLM call, never a second source of truth.

Covers:
  - wait_history: a real durable MainAITaskWait (start_ci_wait()) appears in the task report.
  - next_retry_at + the corrected unresolved_risk semantics: a retryable_failed task with an
    automatically-scheduled retry is no longer flagged unresolved (V0.3 changed this from V0.1's
    behavior -- see app/mainai_execution/final_report.py's _UNRESOLVED_TASK_STATUSES comment).
  - execution_attempt.cancel_requested/cancel_acknowledged surfaced from the real job row.
  - triggered_replan surfaced from a real `replanned` MainAITaskEvent.
  - lesson_conflicts surfaced from a real `lesson_conflict_detected` MainAITaskEvent
    (app/mainai_execution/lesson_conflicts.py's mark_conflict()).
  - summary.plan_versions_total / tasks_with_wait_history / tasks_awaiting_auto_retry /
    tasks_with_disputed_lesson_evidence goal-level rollups."""

from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import executor, final_report, lessons, planner
from app.mainai_execution.ci_wait import start_ci_wait
from app.mainai_execution.lesson_conflicts import mark_conflict
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import (
    EngineeringLessonSeverity,
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
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


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="V0.3 report demo goal", original_instruction="Ship it.", created_by="test")


def _report_for(db_session, task_id) -> dict:
    for t in final_report.generate_goal_report(db_session, goal_id=db_session.get(MainAITask, task_id).goal_id)["tasks"]:
        if t["task_id"] == str(task_id):
            return t
    raise AssertionError("task not found in report")


def test_wait_history_surfaces_a_real_durable_wait(db_session, owner_id):
    planner.create_plan(db_session, goal=_goal(db_session, owner_id), rationale="r", tasks=[PlannedTaskSpec(description="open a pr", task_type="open_pr")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.owner_id == owner_id).one()
    goal = db_session.get(MainAIGoal, task.goal_id)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    db_session.commit()

    report = _report_for(db_session, task.id)
    assert report["task_outcome"] == "waiting_ci"
    assert len(report["wait_history"]) == 1
    assert report["wait_history"][0]["source_type"] == "github_check_runs"
    assert report["wait_history"][0]["status"] == "pending"


def test_next_retry_at_surfaces_and_is_no_longer_unresolved_risk(db_session, owner_id):
    planner.create_plan(db_session, goal=_goal(db_session, owner_id), rationale="r", tasks=[PlannedTaskSpec(description="d", task_type="read_only_audit", max_attempts=3)], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.owner_id == owner_id).one()
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    task.next_retry_at = datetime.utcnow()
    db_session.commit()

    report = _report_for(db_session, task.id)
    assert report["task_outcome"] == "retryable_failed"
    assert report["next_retry_at"] is not None
    assert report["unresolved_risk"] is False


def test_execution_attempt_surfaces_cancel_state_from_the_real_job(db_session, owner_id):
    planner.create_plan(db_session, goal=_goal(db_session, owner_id), rationale="r", tasks=[PlannedTaskSpec(description="d", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.owner_id == owner_id).one()
    goal = db_session.get(MainAIGoal, task.goal_id)
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    job.cancel_requested = True
    db_session.commit()

    report = _report_for(db_session, task.id)
    assert report["execution_attempt"]["cancel_requested"] is True
    assert report["execution_attempt"]["cancel_acknowledged"] is False


def test_triggered_replan_surfaces_a_real_replanned_event(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="d", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    db_session.add(
        MainAITaskEvent(
            task_id=task.id, owner_id=owner_id, event_type=MainAITaskEventType.replanned, detail={"new_plan_version": 2, "provider": "openai", "model": "x"}
        )
    )
    db_session.commit()

    report = _report_for(db_session, task.id)
    assert report["triggered_replan"] == {"new_plan_version": 2, "recorded_at": report["triggered_replan"]["recorded_at"]}


def test_lesson_conflicts_surface_a_real_disputed_lesson(db_session, owner_id):
    a = lessons.record_lesson(
        db_session, problem="p", root_cause="rc", affected_component="app.foo", severity=EngineeringLessonSeverity.medium, evidence="e",
        fix="always X", general_rule="always X", applies_to=["run_tests"], source_type="branch_registry_pass", source_ref="test",
        created_by="test", first_seen_at=datetime.utcnow(), regression_test="tests/a.py::test_a",
    )
    b = lessons.record_lesson(
        db_session, problem="p", root_cause="rc", affected_component="app.foo", severity=EngineeringLessonSeverity.medium, evidence="e",
        fix="never X", general_rule="never X", applies_to=["repo_edit"], source_type="branch_registry_pass", source_ref="test",
        created_by="test", first_seen_at=datetime.utcnow(),
    )
    db_session.commit()

    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="uses lesson a", tasks=[PlannedTaskSpec(description="run tests", task_type="run_tests")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    mark_conflict(db_session, lesson_a=a, lesson_b=b, reasoning="they disagree")
    db_session.commit()

    report = _report_for(db_session, task.id)
    assert len(report["lesson_conflicts"]) == 1
    assert report["lesson_conflicts"][0]["lesson_id"] == str(a.id)
    assert report["unresolved_risk"] is True


def test_goal_summary_rollups(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="d", task_type="read_only_audit")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    task.next_retry_at = datetime.utcnow()
    db_session.commit()

    report = final_report.generate_goal_report(db_session, goal_id=goal.id)
    assert report["summary"]["plan_versions_total"] == 1
    assert report["summary"]["tasks_awaiting_auto_retry"] == 1
    assert report["summary"]["tasks_with_wait_history"] == 0
    assert report["summary"]["tasks_with_disputed_lesson_evidence"] == 0


# --- goal.status waiting rollup ---------------------------------------------------------
#
# MainAIGoalStatus.waiting existed as a schema value with no writer anywhere -- a goal with a
# task genuinely stuck in waiting_ci still read `running`, an honesty gap the founder's own
# review of Cursor's handoff flagged. record_final_report() now rolls this up using the same
# task_statuses it already computes for the terminal-close check.


def test_goal_rolls_up_to_waiting_when_its_task_enters_waiting_ci(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="open a pr", task_type="open_pr")], created_by="test")
    db_session.commit()
    assert goal.status == MainAIGoalStatus.running

    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    db_session.commit()

    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.waiting


def test_goal_rolls_back_to_running_once_the_waiting_task_resumes(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="open a pr", task_type="open_pr")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    db_session.commit()
    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()
    assert goal.status == MainAIGoalStatus.waiting

    # the task resumes to a non-waiting status (matching what resume_waiting_ci_task's own
    # real transitions do -- back to running/ready/a terminal status depending on outcome)
    task.status = MainAITaskStatus.running
    db_session.commit()

    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.running


def test_goal_with_one_waiting_task_among_others_stays_waiting(db_session, owner_id):
    """Matches MainAIGoalStatus.waiting's own docstring: a goal is waiting if ANY of its
    in-flight tasks is -- not only when every task is."""

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="r",
        tasks=[PlannedTaskSpec(description="a", task_type="open_pr"), PlannedTaskSpec(description="b", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()
    tasks = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    waiting_task, other_task = tasks[0], tasks[1]

    job = executor.dispatch_ready_task(db_session, task=waiting_task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    start_ci_wait(db_session, task=waiting_task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    other_task.status = MainAITaskStatus.running
    db_session.commit()

    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.waiting


def test_record_final_report_never_marks_a_blocked_goal_as_waiting(db_session, owner_id):
    """The rollup only ever flips running <-> waiting -- it must never override a goal a
    planner/founder decision already put into blocked (or any other non-running status)."""

    goal = _goal(db_session, owner_id)
    planner.create_plan(db_session, goal=goal, rationale="r", tasks=[PlannedTaskSpec(description="open a pr", task_type="open_pr")], created_by="test")
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha="abc123")
    goal.status = MainAIGoalStatus.blocked
    db_session.commit()

    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.blocked
