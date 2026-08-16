"""Live integration coverage for autonomous gap hardening (PR #79)."""

import hashlib
import subprocess
import sys
from dataclasses import replace

import pytest
from sqlalchemy import select

import app.development_supervisor.service as supervisor_service
from app.autonomous_gap.service import (
    GapGenerationBounds,
    GapGenerationError,
    gap_from_verification_required,
    generate_child_task_for_gap,
    handle_live_gap_signal,
)
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.executor import dispatch_ready_task
from app.development_supervisor.service import (
    SupervisorBounds,
    WorkBinding,
    instruction_sha256,
    run_supervisor,
)
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAIPlan,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.problem_learning import LifeProblem, LifeProblemEvent
from app.safe_planner.service import CandidateStep, PlanCandidate
from tests.backend.mainai.test_scoped_development_supervisor import (
    _foundation,
    _independent_candidate,
)


def _unverified_candidate():
    return PlanCandidate(
        "attempt",
        "change",
        "missing evidence",
        (CandidateStep("gate", "verify", "pass", "verification_evaluate"),),
    )


def _broken_multiply_candidate(original):
    # Keep the broken source a different byte length from the repaired recipe so Python cannot
    # reuse a same-second, same-size stale .pyc between the failing and repaired pytest runs.
    broken = (
        original.rstrip()
        + "\n\ndef multiply(left, right):\n    return left + right + 0\n"
    )
    test = (
        "from calculator import multiply\n\n"
        "def test_multiply():\n"
        "    assert multiply(3, 4) == 12\n"
    )
    return PlanCandidate(
        "implement multiplication with a defective formula",
        "verified multiplication",
        "exercise the real nonretryable focused-test failure path",
        (
            CandidateStep(
                "patch",
                "add multiply helper",
                "source hash",
                "patch_file",
                {
                    "path": "calculator.py",
                    "content": broken,
                    "expected_sha256": hashlib.sha256(original.encode()).hexdigest(),
                },
                required_risk="LOCAL_WRITE",
            ),
            CandidateStep(
                "test-file",
                "add multiplication test",
                "test hash",
                "create_file",
                {
                    "path": "test_calculator.py",
                    "content": test,
                    "expected_sha256": None,
                },
                ("patch",),
                "LOCAL_WRITE",
            ),
            CandidateStep(
                "test",
                "run multiplication test",
                "pytest pass",
                "run_focused_test",
                {"profile_name": "focused_pytest", "arguments": ["test_calculator.py"]},
                ("test-file",),
                "LOCAL_EXECUTION",
                verification_required=True,
            ),
        ),
    )


def _missing_capability_candidate(capability="inspect_git_history"):
    return PlanCandidate(
        "inspect prior repository history",
        "history summary",
        "requires a capability not implemented by the operator dispatch table",
        (CandidateStep("inspect", "inspect history", "history data", capability),),
    )


def _novel_capability_candidate():
    return PlanCandidate(
        "synthesize a novel bounded tool",
        "new deterministic tool",
        "the safe planner must report its concrete missing capability",
        (
            CandidateStep(
                "synthesize",
                "build novel tool",
                "tool exists",
                "synthesize_novel_tool",
            ),
        ),
    )


def _repair_child(db, goal, source_task):
    return (
        db.execute(
            select(MainAITask).where(
                MainAITask.goal_id == goal.id,
                MainAITask.description
                == f"Repair the verification failure blocking: {source_task.description}",
            )
        )
        .scalars()
        .one()
    )


def _created_event(db, task):
    return db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one()


def _gap_checkpoints(db, goal_id):
    return [
        row
        for row in db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal_id)
        )
        .scalars()
        .all()
        if row.executor_state.get("step") == "development_supervisor"
    ]


def _binding(task, prepare, scope, candidate, *, independent=True, allowed_paths=None):
    return WorkBinding(
        task.id,
        prepare,
        candidate,
        independent=independent,
        repository_identity=scope.repository_identity,
        allowed_paths=allowed_paths or scope.allowed_paths,
    )


@pytest.mark.asyncio
async def test_real_failed_verification_repair_without_manual_child_binding(
    superuser_db, tmp_path
):
    owner, goal, first, second, repo, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    original_binding = _binding(
        first,
        prepare,
        scope,
        _broken_multiply_candidate(original),
        independent=False,
    )
    goals_before = (
        superuser_db.query(MainAIGoal)
        .filter(MainAIGoal.owner_id == owner.id)
        .count()
    )

    failed = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(original_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert failed.classification == "FAILED_NONRETRYABLE"
    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
    ).scalar_one()
    child = _repair_child(superuser_db, goal, first)
    assert problem.classification_basis == "deterministic"
    assert first.status == MainAITaskStatus.blocked
    assert child.status == MainAITaskStatus.ready

    approval_gate = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(original_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert approval_gate.classification == "WAITING_APPROVAL"
    assert child.status == MainAITaskStatus.ready

    grant_task_approval(superuser_db, task=child, approved_by="founder")
    repaired = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(original_binding,),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert repaired.classification == "RUN_BOUND_REACHED"
    superuser_db.refresh(child)
    superuser_db.refresh(first)
    assert child.status == MainAITaskStatus.completed
    assert first.status == MainAITaskStatus.completed
    assert "return left * right" in (repo / "calculator.py").read_text()
    assert (repo / "test_calculator.py").exists()
    assert (
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "test_calculator.py"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    assert (
        superuser_db.query(MainAIGoal)
        .filter(MainAIGoal.owner_id == owner.id)
        .count()
        == goals_before
    )


@pytest.mark.asyncio
async def test_takeover_different_worker_lease_converges(superuser_db, tmp_path):
    """Real lease takeover: worker B owns locked_by+generation and re-records the same gap."""
    from app.models.mainai_job import MainAIJobStatus
    from datetime import datetime, timedelta

    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    plan = superuser_db.get(MainAIPlan, first.plan_id)
    job = dispatch_ready_task(
        superuser_db, task=first, goal=goal, dispatched_by="worker-a"
    )
    job.status = MainAIJobStatus.running
    job.locked_by = "worker-a"
    job.lease_generation = 1
    job.started_at = job.started_at or datetime.utcnow()
    job.last_heartbeat_at = datetime.utcnow()
    job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    superuser_db.flush()

    base_ctx = prepare(first, job)
    # prepare() defaults to supervisor-worker — pin lease to worker A.
    job.locked_by = "worker-a"
    job.lease_generation = 1
    job.started_at = job.started_at or datetime.utcnow()
    job.last_heartbeat_at = datetime.utcnow()
    job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    superuser_db.flush()
    context_a = replace(
        base_ctx,
        worker_id="worker-a",
        lease_generation=1,
        allowed_paths=scope.allowed_paths,
    )

    evidence = {
        "signal": "FAILED_NONRETRYABLE",
        "failed_capability": "run_focused_test",
        "verification_required": True,
        "step_arguments": {
            "profile_name": "focused_pytest",
            "arguments": ["test_calculator.py"],
        },
    }
    gap = gap_from_verification_required(
        db=superuser_db,
        scope=scope,
        goal=goal,
        task=first,
        binding_paths=scope.allowed_paths,
        source_job_id=job.id,
        failure_evidence=evidence,
    )
    outcome_a = generate_child_task_for_gap(
        superuser_db,
        scope=scope,
        goal=goal,
        plan=plan,
        gap=gap,
        requested_by="worker-a",
        operator_context=context_a,
    )
    assert outcome_a.classification == "ACCEPTED"
    first_problem = outcome_a.problem
    assert first_problem is not None
    assert outcome_a.inserted_tasks and len(outcome_a.inserted_tasks) == 1
    child_id = outcome_a.inserted_tasks[0].id

    # Worker B takes the lease (generation bump + lock ownership).
    job.locked_by = "worker-b"
    job.lease_generation = 2
    job.last_heartbeat_at = datetime.utcnow()
    job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    superuser_db.flush()
    context_b = replace(
        context_a,
        worker_id="worker-b",
        lease_generation=2,
        allowed_paths=scope.allowed_paths,
    )

    outcome_b = generate_child_task_for_gap(
        superuser_db,
        scope=scope,
        goal=goal,
        plan=plan,
        gap=gap,
        requested_by="worker-b",
        operator_context=context_b,
    )
    assert outcome_b.classification == "ACCEPTED"
    assert outcome_b.problem.id == first_problem.id
    assert [task.id for task in (outcome_b.inserted_tasks or ())] == [child_id]

    problems = (
        superuser_db.execute(
            select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
        )
        .scalars()
        .all()
    )
    assert [problem.id for problem in problems] == [first_problem.id]
    attempt_events = [
        event
        for event in superuser_db.execute(
            select(LifeProblemEvent).where(
                LifeProblemEvent.problem_id == first_problem.id,
                LifeProblemEvent.event_type == "outcome_recorded",
            )
        )
        .scalars()
        .all()
        if (event.detail or {}).get("gap_attempt") is True
    ]
    assert len(attempt_events) >= 2
    assert {event.detail.get("requested_by") for event in attempt_events} >= {
        "worker-a",
        "worker-b",
    }
    superuser_db.refresh(first_problem)
    assert "requested_by" not in (first_problem.provenance or {})

    # Park source and let Supervisor select the single canonical child → approval gate.
    first.status = MainAITaskStatus.blocked
    superuser_db.flush()

    def prepare_worker_b(task, current_job):
        ctx = prepare(task, current_job)
        current_job.locked_by = "worker-b"
        current_job.lease_generation = max(current_job.lease_generation, 2)
        current_job.started_at = current_job.started_at or datetime.utcnow()
        current_job.last_heartbeat_at = datetime.utcnow()
        current_job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
        superuser_db.flush()
        return replace(
            ctx,
            worker_id="worker-b",
            lease_generation=current_job.lease_generation,
            allowed_paths=scope.allowed_paths,
        )

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_binding(first, prepare_worker_b, scope, None),),
        bounds=SupervisorBounds(max_jobs=1),
        worker_id="worker-b",
    )
    assert result.classification == "WAITING_APPROVAL"
    assert (
        len(
            superuser_db.execute(
                select(MainAITask).where(
                    MainAITask.goal_id == goal.id,
                    MainAITask.description
                    == f"Repair the verification failure blocking: {first.description}",
                )
            )
            .scalars()
            .all()
        )
        == 1
    )


@pytest.mark.asyncio
async def test_stale_worker_cannot_write_gap_after_lease_transfer(
    superuser_db, tmp_path, monkeypatch
):
    """TOCTOU race: lease transfers after unlocked preflight, before durable generate."""
    from datetime import datetime, timedelta

    import app.autonomous_gap.service as gap_service

    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    job = dispatch_ready_task(
        superuser_db, task=first, goal=goal, dispatched_by="worker-a"
    )
    context = prepare(first, job)
    # Force known lease identity matching context.
    job.locked_by = context.worker_id
    job.lease_generation = context.lease_generation
    job.started_at = job.started_at or datetime.utcnow()
    job.last_heartbeat_at = datetime.utcnow()
    job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    superuser_db.flush()

    real_generate = gap_service.generate_child_task_for_gap
    stolen = {"done": False}

    def steal_before_durable_write(*args, **kwargs):
        current = superuser_db.get(supervisor_service.MainAIJob, context.job_id)
        current.locked_by = "worker-b"
        current.lease_generation = context.lease_generation + 1
        superuser_db.flush()
        stolen["done"] = True
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(
        gap_service, "generate_child_task_for_gap", steal_before_durable_write
    )

    before_problems = superuser_db.query(LifeProblem).count()
    before_tasks = (
        superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()
    )
    with pytest.raises(gap_service.GapLeaseLostError):
        gap_service.handle_live_gap_signal(
            superuser_db,
            scope=scope,
            goal=goal,
            plan=superuser_db.get(MainAIPlan, first.plan_id),
            task=first,
            classification="VERIFICATION_REQUIRED",
            requested_by="worker-a",
            operator_context=context,
            binding_paths=scope.allowed_paths,
            source_job_id=job.id,
            driver_detail={
                "capability": "verification_evaluate",
                "failed_capability": "verification_evaluate",
                "verification_required": True,
            },
        )
    assert stolen["done"] is True
    assert superuser_db.query(LifeProblem).count() == before_problems
    assert (
        superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()
        == before_tasks
    )


@pytest.mark.asyncio
async def test_narrow_binding_rejects_test_file_write_and_full_envelope_allows(
    superuser_db, tmp_path
):
    """P1 path scope: binding.allowed_paths is enforced at Operator mutation / planning."""
    from sqlalchemy.orm.attributes import flag_modified

    _, goal, first, second, repo, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    test_path = repo / "test_calculator.py"
    assert not test_path.exists()

    # Create a real repair child under the full authorized envelope first.
    failed = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(
                first,
                prepare,
                scope,
                _broken_multiply_candidate(original),
                independent=False,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert failed.classification == "FAILED_NONRETRYABLE"
    child = _repair_child(superuser_db, goal, first)
    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
    ).scalar_one()
    assert (problem.provenance.get("execution_envelope") or {}).get(
        "repair_recipe"
    ) == "multiplication_repair"
    # The broken source plan may already have created the test file — capture pre-repair bytes.
    pre_reject_test = test_path.read_text() if test_path.exists() else None
    pre_reject_calc = (repo / "calculator.py").read_text()
    grant_task_approval(superuser_db, task=child, approved_by="founder")

    def prepare_broad(task, job):
        ctx = prepare(task, job)
        return replace(
            ctx, allowed_paths=("calculator.py", "test_calculator.py")
        )

    # Narrow binding while context still claims both paths — must fail closed.
    envelope = dict(problem.provenance.get("execution_envelope") or {})
    envelope["allowed_paths"] = ["calculator.py"]
    problem.provenance = {
        **problem.provenance,
        "allowed_paths": ["calculator.py"],
        "execution_envelope": envelope,
    }
    flag_modified(problem, "provenance")
    superuser_db.flush()

    narrow_child_binding = WorkBinding(
        child.id,
        prepare_broad,
        None,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=("calculator.py",),
        allow_deterministic_fallback=True,
    )
    rejected = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(narrow_child_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert rejected.classification == "OUT_OF_SCOPE"
    assert (repo / "calculator.py").read_text() == pre_reject_calc
    if pre_reject_test is None:
        assert not test_path.exists()
    else:
        assert test_path.read_text() == pre_reject_test
    superuser_db.refresh(child)
    assert child.status != MainAITaskStatus.completed

    # Both paths explicitly authorized — demo recipe may execute.
    child.status = MainAITaskStatus.ready
    superuser_db.flush()
    grant_task_approval(superuser_db, task=child, approved_by="founder")
    envelope["allowed_paths"] = ["calculator.py", "test_calculator.py"]
    problem.provenance = {
        **problem.provenance,
        "allowed_paths": ["calculator.py", "test_calculator.py"],
        "execution_envelope": envelope,
    }
    flag_modified(problem, "provenance")
    superuser_db.flush()

    wide_binding = WorkBinding(
        child.id,
        prepare,
        None,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=("calculator.py", "test_calculator.py"),
        allow_deterministic_fallback=True,
    )
    allowed = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(wide_binding,),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert allowed.classification in {"RUN_BOUND_REACHED", "COMPLETE"}
    superuser_db.refresh(child)
    assert child.status == MainAITaskStatus.completed
    assert test_path.exists()
    assert "return left * right" in (repo / "calculator.py").read_text()


@pytest.mark.asyncio
async def test_breadth_bounds_are_per_supervisor_invocation(superuser_db, tmp_path):
    """Breadth counters reset each run_supervisor call (not sticky across invocations)."""
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    first.priority = 40
    second.priority = 30
    first_result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(first, prepare, scope, _unverified_candidate()),
            _binding(second, prepare, scope, _unverified_candidate()),
        ),
        bounds=SupervisorBounds(max_jobs=2),
        gap_bounds=GapGenerationBounds(max_children_per_run=1),
    )
    assert first_result.classification == "WAITING_APPROVAL"
    first_state = next(
        cp.executor_state.get("supervisor_state", {})
        for cp in _gap_checkpoints(superuser_db, goal.id)
        if cp.id == first_result.checkpoint_id
    )
    assert first_state.get("gaps_recorded_this_run", 0) >= 1
    assert first_state.get("children_inserted_this_run", 0) >= 1
    problems_after_first = (
        superuser_db.execute(
            select(LifeProblem).where(LifeProblem.owner_id == goal.owner_id)
        )
        .scalars()
        .all()
    )
    assert len(problems_after_first) == 1

    # Park the first lineage (source + repair child) so a fresh invocation can select second.
    first.status = MainAITaskStatus.blocked
    repair_child = _repair_child(superuser_db, goal, first)
    repair_child.status = MainAITaskStatus.blocked
    second.status = MainAITaskStatus.ready
    second.priority = 50
    superuser_db.flush()
    second_result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_binding(second, prepare, scope, _unverified_candidate()),),
        bounds=SupervisorBounds(max_jobs=1),
        gap_bounds=GapGenerationBounds(max_children_per_run=1),
        worker_id="supervisor-worker-resume",
    )
    assert second_result.classification == "WAITING_APPROVAL"
    second_state = next(
        cp.executor_state.get("supervisor_state", {})
        for cp in _gap_checkpoints(superuser_db, goal.id)
        if cp.id == second_result.checkpoint_id
    )
    # Fresh invocation: counters start at zero and may record again (not restored from prior).
    assert second_state.get("gaps_recorded_this_run", 0) >= 1
    assert second_state.get("children_inserted_this_run", 0) >= 1
    problems_after_second = (
        superuser_db.execute(
            select(LifeProblem).where(LifeProblem.owner_id == goal.owner_id)
        )
        .scalars()
        .all()
    )
    assert len(problems_after_second) == 2


@pytest.mark.asyncio
async def test_lease_lost_after_driver_before_gap_insert_no_child(
    superuser_db, tmp_path, monkeypatch
):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    real_run_driver = supervisor_service.run_driver

    def run_then_lose_lease(db, *, context, plan):
        result = real_run_driver(db, context=context, plan=plan)
        job = db.get(supervisor_service.MainAIJob, context.job_id)
        job.lease_generation += 1
        db.flush()
        return result

    monkeypatch.setattr(supervisor_service, "run_driver", run_then_lose_lease)
    before_problems = superuser_db.query(LifeProblem).count()
    before_tasks = (
        superuser_db.query(MainAITask)
        .filter(MainAITask.goal_id == goal.id)
        .count()
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_binding(first, prepare, scope, _unverified_candidate()),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "BLOCKED"
    assert superuser_db.query(LifeProblem).count() == before_problems
    assert (
        superuser_db.query(MainAITask)
        .filter(MainAITask.goal_id == goal.id)
        .count()
        == before_tasks
    )
    assert any(
        row.executor_state["supervisor_state"].get("gap_generation_error")
        == "GapLeaseLostError"
        for row in _gap_checkpoints(superuser_db, goal.id)
    )


@pytest.mark.asyncio
async def test_gap_generation_error_isolates_unrelated_work(
    superuser_db, tmp_path, monkeypatch
):
    _, _, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    first.priority = 20
    second.priority = 10

    def fail_gap_generation(*_args, **_kwargs):
        raise GapGenerationError("injected bounded generation failure")

    monkeypatch.setattr(
        supervisor_service, "handle_live_gap_signal", fail_gap_generation
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(first, prepare, scope, _unverified_candidate()),
            _binding(second, prepare, scope, _independent_candidate()),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert second.status == MainAITaskStatus.completed
    assert result.classification == "RUN_BOUND_REACHED"


@pytest.mark.asyncio
async def test_safe_planner_capability_missing_authorized_self_work(
    superuser_db, tmp_path
):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    instruction = (
        "Improve deterministic development independence within this disposable repository."
    )
    goal.original_instruction = instruction
    self_scope = replace(
        scope,
        self_work=True,
        maximum_risk="medium",
        authorized_instruction_sha256=instruction_sha256(instruction),
    )
    result = await run_supervisor(
        superuser_db,
        scope=self_scope,
        bindings=(
            _binding(first, prepare, self_scope, _novel_capability_candidate()),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "WAITING_APPROVAL"
    child = superuser_db.execute(
        select(MainAITask).where(
            MainAITask.goal_id == goal.id,
            MainAITask.description
            == "Add deterministic support for the missing capability: synthesize_novel_tool",
        )
    ).scalar_one()
    assert child.status == MainAITaskStatus.ready


@pytest.mark.asyncio
async def test_safe_planner_capability_missing_unauthorized(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    first.priority = 20
    second.priority = 10
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(first, prepare, scope, _novel_capability_candidate()),
            _binding(second, prepare, scope, _independent_candidate()),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "RUN_BOUND_REACHED"
    assert second.status == MainAITaskStatus.completed
    assert (
        superuser_db.query(MainAITask)
        .filter(
            MainAITask.goal_id == goal.id,
            MainAITask.description
            == "Add deterministic support for the missing capability: synthesize_novel_tool",
        )
        .count()
        == 0
    )
    checkpoint = next(
        row
        for row in _gap_checkpoints(superuser_db, goal.id)
        if row.executor_state.get("phase") == "CAPABILITY_MISSING"
    )
    assert (
        checkpoint.executor_state["supervisor_state"]["gap_generation"][
            "classification"
        ]
        == "NEEDS_AUTHORIZATION"
    )


@pytest.mark.parametrize("capability", [None, ""])
def test_no_unknown_capability_collapse(superuser_db, tmp_path, capability):
    _, goal, first, _, _, _, _, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    plan = superuser_db.get(MainAIPlan, first.plan_id)
    before_tasks = (
        superuser_db.query(MainAITask)
        .filter(MainAITask.goal_id == goal.id)
        .count()
    )
    outcome = handle_live_gap_signal(
        superuser_db,
        scope=replace(scope, self_work=True, maximum_risk="medium"),
        goal=goal,
        plan=plan,
        task=first,
        classification="CAPABILITY_MISSING",
        capability=capability,
        requested_by="capability-test",
    )
    assert outcome.classification == "CAPABILITY_MISSING"
    assert "refusing" in outcome.reason
    assert "unknown" in outcome.reason
    assert (
        superuser_db.query(MainAITask)
        .filter(MainAITask.goal_id == goal.id)
        .count()
        == before_tasks
    )


def test_fail_closed_lineage_depth(superuser_db, tmp_path):
    _, goal, first, _, _, _, _, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    superuser_db.add(
        MainAITaskEvent(
            task_id=first.id,
            owner_id=first.owner_id,
            event_type=MainAITaskEventType.created,
            detail={
                "insertion_idempotency_key": "autonomous_gap_child:missing-problem"
            },
        )
    )
    superuser_db.flush()
    outcome = handle_live_gap_signal(
        superuser_db,
        scope=scope,
        goal=goal,
        plan=superuser_db.get(MainAIPlan, first.plan_id),
        task=first,
        classification="VERIFICATION_REQUIRED",
        requested_by="lineage-test",
    )
    assert outcome.classification == "DEPTH_BOUND_REACHED"
    assert "unproven or ambiguous" in outcome.reason
    assert superuser_db.query(LifeProblem).count() == 0


@pytest.mark.asyncio
async def test_live_breadth_bound(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    first.priority = 40
    second.priority = 30
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(first, prepare, scope, _unverified_candidate()),
            _binding(second, prepare, scope, _unverified_candidate()),
        ),
        bounds=SupervisorBounds(max_jobs=2),
        gap_bounds=GapGenerationBounds(max_children_per_run=1),
    )
    assert result.classification == "WAITING_APPROVAL"
    problems = (
        superuser_db.execute(
            select(LifeProblem).where(LifeProblem.owner_id == goal.owner_id)
        )
        .scalars()
        .all()
    )
    assert len(problems) == 1
    assert any(
        row.executor_state["supervisor_state"].get("gap_generation", {}).get(
            "classification"
        )
        == "CHILDREN_BOUND_REACHED"
        for row in _gap_checkpoints(superuser_db, goal.id)
    )


@pytest.mark.asyncio
async def test_narrow_path_scope_envelope(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            _binding(
                first,
                prepare,
                scope,
                _unverified_candidate(),
                allowed_paths=("calculator.py",),
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "WAITING_APPROVAL"
    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
    ).scalar_one()
    assert problem.provenance["allowed_paths"] == ["calculator.py"]
    assert problem.provenance["execution_envelope"]["allowed_paths"] == [
        "calculator.py"
    ]
    assert _repair_child(superuser_db, goal, first)


@pytest.mark.asyncio
async def test_repair_child_provenance_and_no_top_level_goal_creation(
    superuser_db, tmp_path
):
    owner, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    goals_before = (
        superuser_db.query(MainAIGoal)
        .filter(MainAIGoal.owner_id == owner.id)
        .count()
    )
    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_binding(first, prepare, scope, _unverified_candidate()),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    child = _repair_child(superuser_db, goal, first)
    created = _created_event(superuser_db, child)
    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
    ).scalar_one()
    attempts = [
        event
        for event in superuser_db.execute(
            select(LifeProblemEvent).where(
                LifeProblemEvent.problem_id == problem.id,
                LifeProblemEvent.event_type == "outcome_recorded",
            )
        )
        .scalars()
        .all()
        if (event.detail or {}).get("gap_attempt") is True
    ]
    assert created.detail["insertion_idempotency_key"].startswith(
        "autonomous_gap_child:"
    )
    assert "requested_by" not in problem.provenance
    assert attempts[-1].detail["requested_by"] == "development-supervisor"
    assert child.goal_id == goal.id
    assert (
        superuser_db.query(MainAIGoal)
        .filter(MainAIGoal.owner_id == owner.id)
        .count()
        == goals_before
    )


def test_non_gap_classifications_can_never_become_a_live_gap(
    superuser_db, tmp_path
):
    _, goal, first, _, _, _, _, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    plan = superuser_db.get(MainAIPlan, first.plan_id)
    for classification in (
        "WAITING_PROVIDER",
        "WAITING_APPROVAL",
        "EXTERNAL_REVIEW_REQUIRED",
        "BLOCKED",
        "CANCELLED",
        "ACTION_BOUND_REACHED",
        "NEEDS_SELECTION",
        "COMPLETE",
    ):
        assert (
            handle_live_gap_signal(
                superuser_db,
                scope=scope,
                goal=goal,
                plan=plan,
                task=first,
                classification=classification,
                requested_by="security-test",
            )
            is None
        )
    assert superuser_db.query(LifeProblem).count() == 0


def test_generated_child_spec_never_carries_a_capability_field(
    superuser_db, tmp_path
):
    import dataclasses

    from app.autonomous_gap.service import propose_child_task_spec

    _, goal, first, _, _, _, _, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    gap = gap_from_verification_required(
        db=superuser_db, scope=scope, goal=goal, task=first
    )
    spec = propose_child_task_spec(gap)
    field_names = {field.name for field in dataclasses.fields(spec)}
    assert "capability" not in field_names
    assert "capabilities" not in field_names
    assert spec.task_type == "repo_edit"
