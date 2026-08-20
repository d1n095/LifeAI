"""MainAI Execution Loop V0.1 — checkpoint/resume (app/mainai_execution/checkpoint.py),
heartbeat/liveness classification (app/mainai_execution/liveness.py), durable final report
generation (app/mainai_execution/final_report.py), and the engineering-lesson/safety-memory
foundation (app/mainai_execution/lessons.py).

Covers, in order:
  A. checkpoint.py: record/lookup round trip, scoped correctly per (task, job, step).
  B. THE checkpoint/resume proof (Demo-3-in-miniature): a simulated worker crash immediately
     after the AI call's result is checkpointed but before the job finishes; a second worker
     reclaims the SAME mainai_jobs row through the real, already-tested lease-expiry reclaim
     path (no special "resume" API) and completes the task WITHOUT a second AI call and
     without a duplicate `completed` event.
  C. liveness.py: task_liveness() is a pure function over (task.status, job.status,
     job.lease_expires_at) — running/stalled/dead distinguished correctly, and
     waiting_external/waiting_ci NEVER reclassified as stalled/dead regardless of job state.
  D. final_report.py: generate_goal_report() keeps execution-attempt status, task outcome,
     verification outcome, and approval state visibly separate for every task, and correctly
     flags unresolved risk; record_final_report() only closes a goal out once every task has
     reached a genuinely terminal status (retryable_failed does NOT count as terminal here —
     it is still actionable via retry_task()).
  E. lessons.py: record/lookup by tag, and the required proof that a real, previously recorded
     lesson actually influences planning — augmenting a persisted task's verification_plan via
     planner.create_plan(), with the lesson's id recorded in the task's own `created` event —
     not a loose, unsourced AI summary.

Real local Postgres (RLS included). Only the LLM provider is faked."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import checkpoint, executor, final_report, graph, lessons, planner
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.liveness import TaskLiveness, task_liveness
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import execute_takeover
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import (
    EngineeringLessonSeverity,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    """Same ordering-trap concern test_mainai_execution_executor.py's identical fixture
    documents: this module writes to both mainai_tasks/mainai_checkpoints/engineering_lessons
    AND mainai_jobs, so both grants must be applied before any test here runs."""
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


def _goal(db_session, owner_id, *, approval_policy="standard_repo_work"):
    return planner.create_goal(
        db_session, owner_id=owner_id, title="Test goal", original_instruction="Do the thing, carefully.", created_by="test", approval_policy=approval_policy
    )


def _single_task_plan(db_session, goal, **spec_kwargs) -> MainAITask:
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "read_only_audit")
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def _mark_terminal(task: MainAITask, status: MainAITaskStatus) -> None:
    task.status = status
    task.completed_at = datetime.utcnow()


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


# ---------------------------------------------------------------- A. checkpoint.py


def test_record_and_lookup_checkpoint_round_trip(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    job_id = uuid.uuid4()

    written = checkpoint.record_checkpoint(db_session, task=task, goal=goal, job_id=job_id, step="work_result", data={"work_result": {"summary": "ok"}})
    db_session.commit()

    found = checkpoint.latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result")
    assert found is not None
    assert found.id == written.id
    assert found.executor_state["work_result"] == {"summary": "ok"}
    assert found.plan_version == goal.current_plan_version


def test_latest_checkpoint_for_step_is_scoped_to_the_exact_job_id(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    job_a, job_b = uuid.uuid4(), uuid.uuid4()

    checkpoint.record_checkpoint(db_session, task=task, goal=goal, job_id=job_a, step="work_result", data={"work_result": "from-a"})
    db_session.commit()

    assert checkpoint.latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_b, step="work_result") is None
    found_a = checkpoint.latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_a, step="work_result")
    assert found_a.executor_state["work_result"] == "from-a"


def test_latest_checkpoint_for_step_distinguishes_steps(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    job_id = uuid.uuid4()

    checkpoint.record_checkpoint(db_session, task=task, goal=goal, job_id=job_id, step="work_result", data={"x": 1})
    checkpoint.record_checkpoint(db_session, task=task, goal=goal, job_id=job_id, step="finalized", data={"x": 2})
    db_session.commit()

    assert checkpoint.latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result").executor_state["x"] == 1
    assert checkpoint.latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="finalized").executor_state["x"] == 2


# ---------------------------------------------------------------- B. crash / resume proof


@pytest.mark.asyncio
async def test_run_task_execution_job_resumes_from_checkpoint_without_repeating_the_ai_call(db_session, superuser_db, owner_id, monkeypatch):
    """The founder's explicit requirement: prove a process restart, prove no duplicate
    execution, and prove resume reads durable checkpoint state rather than in-memory/
    conversation state (there IS no conversation state here -- a second, independent
    run_task_execution_job() call is the closest a test can get to "a different process
    entirely resumed this").

    V0.2 update: `task_execution` jobs are deliberately excluded from
    `claim_next_mainai_job()`'s blind expired-lease reclaim (migration 0034) -- a dead one
    only comes back through the real recovery pipeline (detect -> inspect -> classify ->
    takeover, app/mainai_execution/recovery_takeover.py), which is what this test now drives
    for its "second, independent" attempt, in place of the old bare reclaim. The invariant
    this test proves is unchanged: the durable checkpoint, not any in-memory state, is what a
    genuinely different attempt resumes from."""
    from app.mainai_execution import execution_job

    call_count = {"n": 0}

    async def _counting_chat(self, messages, model, **kwargs):
        call_count["n"] += 1
        return ChatResult(content="Analysen är klar.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _counting_chat)

    real_verify_task = execution_job.verify_task
    verify_calls = {"n": 0}

    def _verify_that_crashes_on_first_call(task, *, cwd):
        verify_calls["n"] += 1
        if verify_calls["n"] == 1:
            raise RuntimeError("simulated worker crash")
        return real_verify_task(task, cwd=cwd)

    monkeypatch.setattr(execution_job, "verify_task", _verify_that_crashes_on_first_call)

    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, task_type="read_only_audit", verification_plan=[])

    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    _, _, generation1 = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)

    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await run_task_execution_job(db_session, job.id, owner_id, worker_id="worker-1", lease_generation=generation1, lease_seconds=120)

    assert call_count["n"] == 1  # the (expensive, non-deterministic) AI call happened exactly once, before the crash

    checkpoint_row = superuser_db.execute(sa_text("SELECT executor_state FROM mainai_checkpoints WHERE task_id = :t"), {"t": str(task.id)}).scalar_one()
    assert checkpoint_row["step"] == "work_result"  # durable -- survived the simulated crash

    crashed_job = superuser_db.get(MainAIJob, job.id)
    assert crashed_job.status == MainAIJobStatus.running  # untouched, exactly what a real crash leaves behind (no worker got to mark it terminal)

    # V0.2: the dead job's lease genuinely expires, but claim_next_mainai_job() will never
    # blind-reclaim it (task_execution is excluded, migration 0034) -- only the real recovery
    # pipeline may revive it.
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()
    assert claim_next_mainai_job(superuser_db, "worker-2", 120) is None  # structurally invisible to blind reclaim

    record = get_or_create_recovery_record(db_session, task=task, job=job)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=task, job=job, record=record)
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification.value == "CHECKPOINTED_WORK"  # real work_result checkpoint, no git/verification evidence yet

    record, new_job = await execute_takeover(db_session, task=task, goal=goal, record=record, dispatched_by="worker-2")
    db_session.commit()

    _, _, generation2 = claim_next_mainai_job(superuser_db, "worker-2", 120)
    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, new_job.id, owner_id, worker_id="worker-2", lease_generation=generation2, lease_seconds=120)

    assert call_count["n"] == 1  # STILL 1 -- resume reused the durable checkpoint, no second AI call
    assert verify_calls["n"] == 2  # verification itself is cheap/idempotent and correctly re-runs

    finished_job = superuser_db.get(MainAIJob, new_job.id)
    assert finished_job.status == MainAIJobStatus.completed
    dead_job = superuser_db.get(MainAIJob, job.id)
    assert dead_job.status == MainAIJobStatus.superseded  # the dead attempt's own honest terminal outcome
    assert dead_job.superseded_by_job_id == new_job.id
    finished_task = superuser_db.get(MainAITask, task.id)
    assert finished_task.status == MainAITaskStatus.completed

    completed_event_count = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type = 'completed'"), {"t": str(task.id)}
    ).scalar()
    assert completed_event_count == 1  # no duplicate completion despite two run_task_execution_job() calls


# ---------------------------------------------------------------- C. liveness.py


class _FakeTask:
    def __init__(self, status):
        self.status = status


class _FakeJob:
    def __init__(self, status, lease_expires_at):
        self.status = status
        self.lease_expires_at = lease_expires_at


NOW = datetime(2026, 1, 1, 12, 0, 0)


@pytest.mark.parametrize("status", [MainAITaskStatus.pending, MainAITaskStatus.ready, MainAITaskStatus.blocked])
def test_task_liveness_idle_when_no_execution_is_in_flight(status):
    assert task_liveness(_FakeTask(status), None, now=NOW) == TaskLiveness.idle


def test_task_liveness_running_when_lease_still_valid():
    job = _FakeJob(MainAIJobStatus.running, NOW + timedelta(seconds=60))
    assert task_liveness(_FakeTask(MainAITaskStatus.running), job, now=NOW) == TaskLiveness.running


def test_task_liveness_stalled_when_lease_expired_but_not_yet_reclaimed():
    job = _FakeJob(MainAIJobStatus.running, NOW - timedelta(seconds=1))
    assert task_liveness(_FakeTask(MainAITaskStatus.running), job, now=NOW) == TaskLiveness.stalled


def test_task_liveness_dead_when_the_linked_job_is_missing():
    assert task_liveness(_FakeTask(MainAITaskStatus.running), None, now=NOW) == TaskLiveness.dead


def test_task_liveness_dead_when_the_linked_job_already_reached_a_terminal_state():
    job = _FakeJob(MainAIJobStatus.completed, NOW + timedelta(seconds=60))
    assert task_liveness(_FakeTask(MainAITaskStatus.running), job, now=NOW) == TaskLiveness.dead


def test_task_liveness_never_misclassifies_waiting_external_as_stalled_or_dead():
    assert task_liveness(_FakeTask(MainAITaskStatus.waiting_external), None, now=NOW) == TaskLiveness.waiting_external
    stalled_job = _FakeJob(MainAIJobStatus.running, NOW - timedelta(seconds=1))
    assert task_liveness(_FakeTask(MainAITaskStatus.waiting_external), stalled_job, now=NOW) == TaskLiveness.waiting_external


def test_task_liveness_never_misclassifies_waiting_ci_as_stalled_or_dead():
    assert task_liveness(_FakeTask(MainAITaskStatus.waiting_ci), None, now=NOW) == TaskLiveness.waiting_ci


@pytest.mark.parametrize("status", [MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled, MainAITaskStatus.retryable_failed])
def test_task_liveness_done_for_every_terminal_or_retry_exhausted_status(status):
    assert task_liveness(_FakeTask(status), None, now=NOW) == TaskLiveness.done


# ---------------------------------------------------------------- D. final_report.py


def test_generate_goal_report_raises_for_an_unknown_goal(db_session):
    with pytest.raises(final_report.GoalNotFoundError):
        final_report.generate_goal_report(db_session, goal_id=uuid.uuid4())


def test_generate_goal_report_keeps_outcome_verification_and_approval_separate(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="mixed outcomes",
        tasks=[
            PlannedTaskSpec(description="completed, verified", task_type="read_only_audit"),
            PlannedTaskSpec(description="needs approval, not yet granted", task_type="repo_edit", approval_required=True),
            PlannedTaskSpec(description="will fail", task_type="read_only_audit"),
            PlannedTaskSpec(description="blocked", task_type="read_only_audit", depends_on=[2]),
        ],
        created_by="test",
    )
    db_session.commit()

    completed_task, _approval_task, failing_task, _blocked_spec_task = (
        db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    )

    db_session.add(MainAITaskEvent(task_id=completed_task.id, owner_id=owner_id, event_type=MainAITaskEventType.verification_passed, detail={"passed": True}))
    _mark_terminal(completed_task, MainAITaskStatus.completed)
    _mark_terminal(failing_task, MainAITaskStatus.failed)
    db_session.flush()
    graph.recompute_task_readiness(db_session, goal_id=goal.id)  # promotes/blocks dependents, exactly like the real executor would after a task finishes
    db_session.commit()

    report = final_report.generate_goal_report(db_session, goal_id=goal.id)

    by_desc = {t["description"]: t for t in report["tasks"]}
    completed_report = by_desc["completed, verified"]
    assert completed_report["task_outcome"] == "completed"
    assert completed_report["verification_outcome"]["passed"] is True
    assert completed_report["unresolved_risk"] is False

    approval_report = by_desc["needs approval, not yet granted"]
    assert approval_report["approval"] == {"required": True, "granted": False, "granted_by": None}
    assert approval_report["unresolved_risk"] is True  # approval required but not granted

    blocked_report = by_desc["blocked"]
    assert blocked_report["task_outcome"] == "blocked"
    assert blocked_report["unresolved_risk"] is True

    assert report["summary"]["total_tasks"] == 4
    assert report["summary"]["unresolved_risk_count"] == 3  # approval-unresolved + failed + blocked


def test_record_final_report_closes_the_goal_only_once_every_task_is_genuinely_terminal(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)

    # Not yet terminal (still `ready`) -- record_final_report() must leave the goal alone.
    report = final_report.record_final_report(db_session, goal=goal)
    db_session.commit()
    assert goal.final_outcome is None
    assert goal.status == MainAIGoalStatus.running
    assert report["tasks"][0]["task_outcome"] == "ready"

    # retryable_failed is deliberately NOT terminal here -- still actionable via retry_task().
    task.status = MainAITaskStatus.retryable_failed
    task.attempts = 1
    db_session.commit()
    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()
    assert goal.final_outcome is None

    _mark_terminal(task, MainAITaskStatus.completed)
    db_session.commit()
    report = final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.completed
    assert goal.completed_at is not None
    assert goal.final_outcome is not None
    import json

    assert json.loads(goal.final_outcome) == report


def test_record_final_report_marks_the_goal_failed_if_any_task_ends_up_failed(db_session, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal)
    _mark_terminal(task, MainAITaskStatus.failed)
    db_session.commit()

    final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.failed


# ---------------------------------------------------------------- E. lessons.py


def _record_lesson(
    db_session,
    *,
    applies_to,
    regression_test=None,
    severity=EngineeringLessonSeverity.medium,
    affected_component="mainai_execution.planner",
):
    return lessons.record_lesson(
        db_session,
        problem="A repo_edit task once shipped without running its own regression test.",
        root_cause="Planner did not attach a targeted_tests step for repo_edit by default.",
        affected_component=affected_component,
        severity=severity,
        evidence="See PR history for the incident this codifies.",
        fix="Always attach the relevant regression test to repo_edit tasks touching this area.",
        general_rule="A repo_edit task must always verify itself with a real targeted test.",
        applies_to=applies_to,
        source_type="branch_registry_pass",
        source_ref="Pass N (test fixture)",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test=regression_test,
    )


def test_record_and_lookup_lessons_by_tag(db_session, owner_id):
    matching = _record_lesson(db_session, applies_to=["repo_edit"], severity=EngineeringLessonSeverity.high)
    _record_lesson(db_session, applies_to=["open_pr"], severity=EngineeringLessonSeverity.low)
    db_session.commit()

    found = lessons.lookup_lessons(db_session, applies_to_any=["repo_edit"])
    assert [lesson.id for lesson in found] == [matching.id]


def test_lookup_lessons_returns_empty_for_no_tags(db_session, owner_id):
    assert lessons.lookup_lessons(db_session, applies_to_any=[]) == []


def test_apply_lessons_to_verification_plan_adds_a_missing_regression_test(db_session, owner_id):
    lesson = _record_lesson(db_session, applies_to=["repo_edit"], regression_test="tests/backend/test_x.py")
    db_session.commit()

    plan, applied_ids = lessons.apply_lessons_to_verification_plan(db_session, task_type="repo_edit", verification_plan=[])

    assert plan == [{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}]
    assert applied_ids == [lesson.id]


def test_apply_lessons_to_verification_plan_never_duplicates_an_existing_target(db_session, owner_id):
    _record_lesson(db_session, applies_to=["repo_edit"], regression_test="tests/backend/test_x.py")
    db_session.commit()

    plan, applied_ids = lessons.apply_lessons_to_verification_plan(
        db_session, task_type="repo_edit", verification_plan=[{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}]
    )

    assert plan == [{"kind": "targeted_tests", "target": "tests/backend/test_x.py"}]
    assert applied_ids == []


def test_apply_lessons_to_verification_plan_ignores_a_lesson_for_a_different_task_type(db_session, owner_id):
    _record_lesson(db_session, applies_to=["open_pr"], regression_test="tests/backend/test_x.py")
    db_session.commit()

    plan, applied_ids = lessons.apply_lessons_to_verification_plan(db_session, task_type="repo_edit", verification_plan=[])
    assert plan == []
    assert applied_ids == []


@pytest.mark.parametrize("bad_target", ["/etc/passwd", "../secrets.py", "tests/../../etc/shadow", ""])
def test_apply_lessons_to_verification_plan_skips_unsafe_regression_targets(db_session, owner_id, bad_target):
    """Plan-time fail-closed: create_plan validates AI verification targets before lesson
    apply, so an absolute/`..` regression_test on a lesson previously bypassed that gate and
    only failed at the subprocess boundary. Unsafe lesson targets must be skipped, never
    persisted onto the plan."""
    _record_lesson(db_session, applies_to=["repo_edit"], regression_test=bad_target, affected_component="mainai_execution.planner.bad")
    safe = _record_lesson(
        db_session,
        applies_to=["repo_edit"],
        regression_test="tests/backend/test_safe.py",
        affected_component="mainai_execution.planner.safe",
    )
    db_session.commit()

    plan, applied_ids = lessons.apply_lessons_to_verification_plan(db_session, task_type="repo_edit", verification_plan=[])

    assert plan == [{"kind": "targeted_tests", "target": "tests/backend/test_safe.py"}]
    assert applied_ids == [safe.id]


def test_apply_lessons_skips_deterministic_conflict_candidate_pairs(db_session, owner_id):
    """Unresolved conflict candidates must not both inject regression tests at plan time.
    Same affected_component + overlapping applies_to is enough to quarantine both until the
    async conflict tick (or founder) resolves them — no AI judgment required for the gate."""
    a = _record_lesson(
        db_session,
        applies_to=["repo_edit"],
        regression_test="tests/backend/test_a.py",
        affected_component="mainai_execution.verify",
    )
    b = _record_lesson(
        db_session,
        applies_to=["repo_edit"],
        regression_test="tests/backend/test_b.py",
        affected_component="mainai_execution.verify",
    )
    alone = _record_lesson(
        db_session,
        applies_to=["repo_edit"],
        regression_test="tests/backend/test_alone.py",
        affected_component="mainai_execution.planner.unrelated",
    )
    db_session.commit()

    plan, applied_ids = lessons.apply_lessons_to_verification_plan(db_session, task_type="repo_edit", verification_plan=[])

    assert plan == [{"kind": "targeted_tests", "target": "tests/backend/test_alone.py"}]
    assert applied_ids == [alone.id]
    assert a.id not in applied_ids and b.id not in applied_ids


def test_create_plan_is_actually_influenced_by_a_real_previously_recorded_lesson(db_session, owner_id):
    """The founder's explicit demo requirement: at least one real previous lesson must
    influence planning or verification -- not a loose AI summary with no source and no
    effect. This proves it end to end through the real planner entry point."""
    lesson = _record_lesson(db_session, applies_to=["repo_edit"], regression_test="tests/backend/test_lesson_regression.py")
    db_session.commit()

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="a repo_edit task with no verification_plan of its own",
        tasks=[PlannedTaskSpec(description="edit a file", task_type="repo_edit")],
        created_by="test",
    )
    db_session.commit()

    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    assert {"kind": "targeted_tests", "target": "tests/backend/test_lesson_regression.py"} in task.verification_plan

    created_event = db_session.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :t AND event_type = 'created'"), {"t": str(task.id)}
    ).scalar_one()
    assert created_event["lessons_applied"] == [str(lesson.id)]
