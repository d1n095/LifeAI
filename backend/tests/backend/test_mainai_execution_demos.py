"""MainAI Execution Loop V0.1 — the four required demos, run through the REAL implementation
end to end (no manual shortcut around the loop): a goal is created, planned, and then driven
to completion purely by repeated Worker().run_once() poll cycles -- the same poll loop that
would run in production, not a test-only bypass. Only the LLM provider is ever faked; the DB,
RLS, the task graph, the approval gate, verification, checkpoints, and the worker's own
auto-advance tick (app/worker.py's _advance_mainai_execution_tasks(), added specifically to
make this loop self-propelling instead of requiring an external caller to dispatch every task
by hand) are all real.

Covers, in order:
  A. _advance_mainai_execution_tasks() in isolation: dispatches every independently-ready task
     across owners, and correctly leaves an approval_required task `ready`-but-undispatched
     (never silently satisfies the gate) while still advancing everything else.
  B. Demo 1 -- success path: "Gör en read-only repo audit, hitta en mycket liten
     behavior-neutral dokumentationsfix, genomför den, verifiera den och öppna en PR." Goal ->
     AI-proposed plan -> create_plan() -> the worker's own poll loop dispatches and executes
     read_only_audit -> repo_edit -> run_tests -> open_pr in dependency order, with zero manual
     dispatch calls from the test itself. github_write_enabled stays at its documented default
     (False, no credentials configured in this environment) -- open_pr correctly produces a
     durable, evidence-backed PR PROPOSAL (title/body/head/base), not a live network call; this
     is the honest, already-approved behavior, not a shortcut. Ends with a truthful
     record_final_report().
  C. Demo 2 -- failure path: a deliberately failing run_tests task proves the task lands on
     retryable_failed (never completed), its dependent is never falsely advanced, and
     record_final_report() tells the truth about it (unresolved_risk, no false "success").
  D. Demo 3 -- restart/resume: a simulated crash mid-task. V0.2 update (migration 0034):
     `task_execution` jobs are deliberately excluded from `claim_next_mainai_job()`'s blind
     expired-lease reclaim -- Worker().run_once() alone can no longer revive a dead one, only
     the real recovery pipeline can (detect -> inspect -> classify -> takeover,
     app/mainai_execution/recovery_takeover.py). This demo now shows both halves working
     together: the recovery pipeline creates the new attempt (proving the dead job is
     structurally invisible to blind reclaim first), and Worker().run_once() then picks up
     THAT new, ordinary queued job and drives it to completion through the real poll loop --
     the AI provider is still called exactly once overall and there is still no duplicate
     terminal event, which is the guarantee this demo exists to prove.
  E. Demo 4 -- engineering lesson: a REAL historical incident from this project's own history
     (BLOCKER: implement real lease fencing for mainai_jobs -- see docs/BRANCH_REGISTRY.md /
     the task tracker's own #323) is recorded as an EngineeringLesson with full provenance,
     shown to be found by tag lookup, shown to actually influence a new plan's
     verification_plan, and shown to be structurally separate from user-private memory
     (engineering_lessons has no owner_id / RLS policy at all -- see
     app/models/mainai_execution.py's EngineeringLesson docstring).

Real local Postgres (RLS included). Only the LLM provider is faked."""

import json
from datetime import datetime

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import final_report, lessons, planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import execute_takeover
from app.models.mainai_execution import (
    EngineeringLessonConfidence,
    EngineeringLessonSeverity,
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker

TERMINAL_OR_RETRYING = frozenset(
    {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled, MainAITaskStatus.retryable_failed}
)


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
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


def _goal(db_session, owner_id, *, title="Demo goal", instruction="Do the thing, carefully."):
    return planner.create_goal(db_session, owner_id=owner_id, title=title, original_instruction=instruction, created_by="test")


def _single_task_plan(db_session, goal, **spec_kwargs) -> MainAITask:
    spec_kwargs.setdefault("description", "do the work")
    spec_kwargs.setdefault("task_type", "read_only_audit")
    planner.create_plan(db_session, goal=goal, rationale="single task", tasks=[PlannedTaskSpec(**spec_kwargs)], created_by="test")
    db_session.commit()
    return db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


async def _run_until_no_terminal_tasks_remain(superuser_db, goal_id, *, max_iterations=25):
    """Drives the REAL worker poll loop -- the same Worker().run_once() production would run --
    until every task belonging to `goal_id` has reached a terminal-or-retrying status, or gives
    up after `max_iterations` (a bug that stalls the loop should fail the test loudly, not hang
    it forever)."""
    for _ in range(max_iterations):
        worked = await Worker().run_once()
        superuser_db.expire_all()  # a separate session/connection did the writes -- never trust this session's identity map without refreshing
        tasks = superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()
        if tasks and all(t.status in TERMINAL_OR_RETRYING for t in tasks):
            return tasks
        if not worked:
            # Nothing claimed AND nothing left to auto-advance -- if tasks aren't all
            # terminal yet, the loop has genuinely stalled; let the final assertion below
            # report exactly what state it stalled in rather than looping uselessly.
            break
    superuser_db.expire_all()
    return superuser_db.execute(select(MainAITask).where(MainAITask.goal_id == goal_id)).scalars().all()


def _fake_chat_router(*, plan_json: str | None = None, edit_json: str | None = None, audit_text: str = "Granskningen visar inga problem."):
    async def _chat(self, messages, model, **kwargs):
        system = messages[0].content if messages else ""
        if plan_json is not None and "planerare" in system:
            return ChatResult(content=plan_json, provider="openai", model=model, raw_usage={})
        if edit_json is not None and "kodagent" in system:
            return ChatResult(content=edit_json, provider="openai", model=model, raw_usage={})
        return ChatResult(content=audit_text, provider="openai", model=model, raw_usage={})

    return _chat


# ---------------------------------------------------------------- A. auto-advance tick


@pytest.mark.asyncio
async def test_advance_mainai_execution_tasks_dispatches_ready_tasks_and_leaves_approval_gated_ones_alone(db_session, superuser_db, owner_id):
    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="two independent tasks, one gated",
        tasks=[
            PlannedTaskSpec(description="auto task", task_type="read_only_audit"),
            PlannedTaskSpec(description="gated task", task_type="read_only_audit", approval_required=True),
        ],
        created_by="test",
    )
    db_session.commit()

    auto_task, gated_task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at).all()
    assert auto_task.status == MainAITaskStatus.ready
    assert gated_task.status == MainAITaskStatus.ready

    Worker()._advance_mainai_execution_tasks(superuser_db)

    superuser_db.expire_all()
    auto_task = superuser_db.get(MainAITask, auto_task.id)
    gated_task = superuser_db.get(MainAITask, gated_task.id)
    assert auto_task.status == MainAITaskStatus.running  # dispatched -- no approval needed for this policy/task_type
    assert auto_task.mainai_job_id is not None
    assert gated_task.status == MainAITaskStatus.ready  # untouched -- approval gate stopped it, no job created
    assert gated_task.mainai_job_id is None

    jobs_for_gated = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o AND id != :dispatched"), {"o": str(owner_id), "dispatched": str(auto_task.mainai_job_id)}).scalar()
    assert jobs_for_gated == 0


@pytest.mark.asyncio
async def test_advance_mainai_execution_tasks_dispatches_a_gated_task_once_approval_is_granted(db_session, superuser_db, owner_id):
    goal = _goal(db_session, owner_id)
    task = _single_task_plan(db_session, goal, approval_required=True)
    grant_task_approval(db_session, task=task, approved_by="founder")
    db_session.commit()

    Worker()._advance_mainai_execution_tasks(superuser_db)

    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.running


# ---------------------------------------------------------------- B. Demo 1 -- success path


_DEMO1_PLAN_JSON = json.dumps(
    {
        "tasks": [
            {"description": "Läs igenom det aktuella modulträdet och hitta en liten, behavior-neutral dokumentations- eller testreferensfix.", "task_type": "read_only_audit", "depends_on": []},
            {
                "description": "Genomför den lilla, avgränsade fixen.",
                "task_type": "repo_edit",
                "depends_on": [0],
                "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_demo_doc_fix.py"}],
            },
            {
                "description": "Kör de riktade testerna som verifierar fixen.",
                "task_type": "run_tests",
                "depends_on": [1],
                "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_demo_doc_fix.py"}],
            },
            {"description": "Öppna en PR för den verifierade fixen.", "task_type": "open_pr", "depends_on": [2]},
        ]
    }
)

_DEMO1_EDIT_JSON = json.dumps(
    {
        "files": [{"path": "backend/tests/test_demo_doc_fix.py", "content": "def test_demo_doc_fix():\n    assert True\n"}],
        "commit_message": "Fix stale doc reference (MainAI V0.1 success demo)",
    }
)


@pytest.mark.asyncio
async def test_demo_1_full_success_path_end_to_end_through_the_real_worker_poll_loop(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_router(plan_json=_DEMO1_PLAN_JSON, edit_json=_DEMO1_EDIT_JSON))

    goal = _goal(
        db_session,
        owner_id,
        title="Doc reference audit + fix",
        instruction="Gör en read-only repo audit, hitta en mycket liten behavior-neutral dokumentations- eller testreferensfix, genomför den, verifiera den och öppna en PR.",
    )
    specs, provider, _model = await planner.propose_plan_via_ai(db_session, goal=goal)
    assert provider == "openai"
    assert [s.task_type for s in specs] == ["read_only_audit", "repo_edit", "run_tests", "open_pr"]

    planner.create_plan(db_session, goal=goal, rationale="AI-proposed doc fix plan", tasks=specs, created_by="test")
    db_session.commit()

    # From here on, NOTHING in this test calls dispatch_ready_task()/run_task_execution_job()
    # directly -- the real worker poll loop does every dispatch and every execution.
    tasks = await _run_until_no_terminal_tasks_remain(superuser_db, goal.id)

    assert len(tasks) == 4
    by_type = {t.task_type: t for t in tasks}
    for task_type, task in by_type.items():
        assert task.status == MainAITaskStatus.completed, f"{task_type} ended as {task.status.value}, not completed"

    written = tmp_path / "backend" / "tests" / "test_demo_doc_fix.py"
    assert written.exists()
    assert "assert True" in written.read_text()

    open_pr_event = superuser_db.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :t AND event_type = 'completed'"), {"t": str(by_type["open_pr"].id)}
    ).scalar_one()
    # github_write_enabled stays at its documented default (False) in this environment -- a
    # real, durable, evidence-backed PROPOSAL is the correct and honest outcome here, not a
    # live network call (see this module's own docstring, section B).
    assert open_pr_event["work_result"]["proposed"] is True
    assert open_pr_event["work_result"]["head"] == f"claude/mainai-task-{by_type['repo_edit'].id}"

    _set_rls_user(db_session, owner_id)
    db_session.expire_all()
    goal = db_session.get(type(goal), goal.id)
    report = final_report.record_final_report(db_session, goal=goal)
    db_session.commit()

    assert goal.status == MainAIGoalStatus.completed
    assert report["summary"]["unresolved_risk_count"] == 0
    assert all(t["task_outcome"] == "completed" for t in report["tasks"])


# ---------------------------------------------------------------- C. Demo 2 -- failure path


@pytest.mark.asyncio
async def test_demo_2_failure_path_never_falsely_completes_and_the_report_tells_the_truth(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_demo_failing.py").write_text("def test_demo_failing():\n    assert False\n")

    goal = _goal(db_session, owner_id, title="Deliberately failing verification demo")
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="a failing run_tests task with a dependent",
        tasks=[
            PlannedTaskSpec(
                description="run the deliberately failing suite",
                task_type="run_tests",
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_demo_failing.py"}],
            ),
            PlannedTaskSpec(description="depends on the failing task", task_type="read_only_audit", depends_on=[0]),
        ],
        created_by="test",
    )
    db_session.commit()

    tasks = await _run_until_no_terminal_tasks_remain(superuser_db, goal.id)
    by_type_desc = {t.description: t for t in tasks}

    failing_task = by_type_desc["run the deliberately failing suite"]
    dependent_task = by_type_desc["depends on the failing task"]

    assert failing_task.status == MainAITaskStatus.retryable_failed  # the execution ATTEMPT finished; the task did NOT succeed
    assert failing_task.completed_at is None
    dependent_events = superuser_db.execute(sa_text("SELECT event_type FROM mainai_task_events WHERE task_id = :t"), {"t": str(dependent_task.id)}).scalars().all()
    assert dependent_task.status == MainAITaskStatus.pending  # never falsely advanced -- retryable_failed is not terminal
    assert MainAITaskEventType.dispatched.value not in dependent_events

    _set_rls_user(db_session, owner_id)
    db_session.expire_all()
    goal = db_session.get(type(goal), goal.id)
    report = final_report.generate_goal_report(db_session, goal_id=goal.id)  # record_final_report() would correctly refuse to close this goal -- retryable_failed isn't terminal

    failing_report = next(t for t in report["tasks"] if t["task_id"] == str(failing_task.id))
    assert failing_report["task_outcome"] == "retryable_failed"
    assert failing_report["verification_outcome"]["passed"] is False
    assert failing_report["unresolved_risk"] is True
    assert report["summary"]["unresolved_risk_count"] >= 1


# ---------------------------------------------------------------- D. Demo 3 -- restart / resume


@pytest.mark.asyncio
async def test_demo_3_restart_resume_through_the_real_worker_poll_loop(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """The RESUME half runs entirely through the real Worker().run_once() poll loop -- the
    same production code path that would reclaim and finish an abandoned job. The CRASH half
    is deliberately NOT driven through Worker().run_once(): app/worker.py's
    process_claimed_mainai_job() has its own broad except-Exception handler, which exists to
    turn a genuine application BUG into a truthful mark_failed() job outcome (correct
    behavior) -- but that is not the same event as a real process crash (kill -9 / OOM / host
    reboot), which never gives Python's try/except a chance to run at all. Calling
    run_task_execution_job() directly, the way test_mainai_execution_resilience.py's own
    narrower proof does, is the most faithful simulation of a genuine crash available in a
    unit test; dispatch and reclaim both still go through real, unmodified production code."""
    from app.mainai_execution import execution_job

    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)

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

    goal = _goal(db_session, owner_id, title="Restart/resume demo")
    task = _single_task_plan(db_session, goal, task_type="read_only_audit", verification_plan=[])

    # Dispatch through the real auto-advance tick (the exact mechanism Worker().run_once()
    # itself calls every poll cycle -- see app/worker.py's _advance_mainai_execution_tasks()).
    Worker()._advance_mainai_execution_tasks(superuser_db)
    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    job_id = task.mainai_job_id
    assert job_id is not None

    _, _, generation1 = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, owner_id)
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=generation1, lease_seconds=120)

    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    assert task.status == MainAITaskStatus.running  # untouched -- exactly what a real crash leaves behind
    checkpoint_row = superuser_db.execute(sa_text("SELECT executor_state FROM mainai_checkpoints WHERE task_id = :t"), {"t": str(task.id)}).scalar_one()
    assert checkpoint_row["step"] == "work_result"
    assert superuser_db.get(MainAIJob, job_id).status == MainAIJobStatus.running

    # V0.2: the dead job's lease genuinely expires, but it is structurally invisible to
    # Worker().run_once()'s own blind reclaim (task_execution excluded, migration 0034) --
    # only the real recovery pipeline may revive it.
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job_id)})
    superuser_db.commit()
    assert claim_next_mainai_job(superuser_db, "some-other-worker", 120) is None

    goal_row = superuser_db.get(MainAIGoal, task.goal_id)
    record = get_or_create_recovery_record(superuser_db, task=task, job=superuser_db.get(MainAIJob, job_id))
    superuser_db.commit()
    record = await inspect_recovery_record(superuser_db, task=task, job=superuser_db.get(MainAIJob, job_id), record=record)
    superuser_db.commit()
    record = classify_recovery_record(superuser_db, record=record)
    superuser_db.commit()
    assert record.classification.value == "CHECKPOINTED_WORK"

    record, new_job = execute_takeover(superuser_db, task=task, goal=goal_row, record=record, dispatched_by="recovery-worker")
    superuser_db.commit()

    # The new attempt is now an ordinary `queued` job -- Worker().run_once() picks it up and
    # drives it to completion through the real, unmodified production poll loop, exactly like
    # any other job.
    worked = await Worker().run_once()
    assert worked is True

    superuser_db.expire_all()
    task = superuser_db.get(MainAITask, task.id)
    job = superuser_db.get(MainAIJob, new_job.id)
    dead_job = superuser_db.get(MainAIJob, job_id)
    assert job.status == MainAIJobStatus.completed
    assert dead_job.status == MainAIJobStatus.superseded
    assert dead_job.superseded_by_job_id == new_job.id
    assert task.status == MainAITaskStatus.completed
    assert call_count["n"] == 1  # resumed from the durable checkpoint -- no second AI call
    assert verify_calls["n"] == 2

    completed_event_count = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type = 'completed'"), {"t": str(task.id)}
    ).scalar()
    assert completed_event_count == 1


# ---------------------------------------------------------------- E. Demo 4 -- engineering lesson


def test_demo_4_a_real_historical_lesson_influences_planning_and_stays_separate_from_user_private_memory(db_session, owner_id):
    """The lesson recorded here is a REAL incident from this project's own history (see the
    task tracker's #322-323 and docs/BRANCH_REGISTRY.md's own Pass entries for PR #36's founder
    re-review round): a prior version of the mainai_jobs runtime had NO real lease fencing, so
    two racing workers could both believe they owned the same job -- exactly the class of
    "dead-agent/lost-work" risk this V0.1 execution loop's own approval/verification/checkpoint
    invariants exist to prevent from recurring. Its regression_test names the REAL test that
    still guards this in this codebase today."""
    lesson = lessons.record_lesson(
        db_session,
        problem="A worker whose claim on a mainai_jobs row was reclaimed (lease expired) could still race a second worker for the same job, both believing they alone owned it.",
        root_cause="Early mainai_jobs writes were not fenced by a lease_generation token checked atomically in the same UPDATE that performed the write.",
        affected_component="app.jobs.mainai_job_lease / app.jobs.service",
        severity=EngineeringLessonSeverity.critical,
        evidence="Founder re-review round on PR #36 (task tracker #323, 'BLOCKER: implement real lease fencing for mainai_jobs').",
        fix="Every worker-driven write to a claimed mainai_jobs row now goes through _guarded_job_write(), which re-verifies worker_id AND lease_generation atomically in the same UPDATE statement.",
        general_rule="Any durable execution unit with a lease/claim (mainai_jobs, and by extension MainAI Execution Loop V0.1's task_execution jobs) must fence every write with the exact lease token the claim returned -- never trust an in-memory 'I still own this' assumption.",
        applies_to=["task_execution", "mainai_jobs", "lease_fencing", "repo_edit"],
        source_type="branch_registry_pass",
        source_ref="PR #36 founder re-review round -- BLOCKER: implement real lease fencing for mainai_jobs (task tracker #323)",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/jobs/test_mainai_jobs.py::test_two_workers_racing_many_jobs_never_claim_the_same_job",
        confidence=EngineeringLessonConfidence.certain,
    )
    db_session.commit()

    found = lessons.lookup_lessons(db_session, applies_to_any=["task_execution"])
    assert lesson.id in [row.id for row in found]

    goal = _goal(db_session, owner_id, title="A task_execution-related change")
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="repo_edit task in a task_execution-related area",
        tasks=[PlannedTaskSpec(description="Touch task_execution dispatch logic", task_type="repo_edit")],
        created_by="test",
    )
    db_session.commit()

    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    assert {"kind": "targeted_tests", "target": "tests/backend/jobs/test_mainai_jobs.py::test_two_workers_racing_many_jobs_never_claim_the_same_job"} in task.verification_plan

    created_event = db_session.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :t AND event_type = 'created'"), {"t": str(task.id)}
    ).scalar_one()
    assert str(lesson.id) in created_event["lessons_applied"]

    # Structural separation from user-private memory: engineering_lessons carries no owner_id
    # column and no RLS policy at all -- it is founder-wide project knowledge, never scoped to
    # or mixable with any individual user's private data (see
    # app/models/mainai_execution.py's EngineeringLesson docstring).
    columns = {c["name"] for c in sa_inspect(db_session.bind).get_columns("engineering_lessons")}
    assert "owner_id" not in columns
    rls_enabled = db_session.execute(
        sa_text("SELECT relrowsecurity FROM pg_class WHERE relname = 'engineering_lessons'")
    ).scalar()
    assert rls_enabled is False
