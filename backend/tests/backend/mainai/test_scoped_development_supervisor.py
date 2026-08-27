import hashlib
import json
import subprocess
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.development_operator.service import OperatorContext
from app.development_supervisor.service import (
    SupervisorBounds,
    SupervisorError,
    SupervisorScope,
    WorkBinding,
    discover_candidates,
    instruction_sha256,
    record_founder_correction,
    run_supervisor,
    validate_scope,
)
from app.intelligence_governance import record_execution
from app.mainai_execution.approval import grant_task_approval
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAIGoalStatus,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskDependency,
    MainAITaskStatus,
)
from app.models.intelligence_governance import IntelligenceEvidence
from app.models.mainai_job import MainAIJobStatus
from app.models.mainai_recovery import MainAITaskWorktree
from app.models.user import User
from app.models.work_intelligence import WorkTraceEvent
from app.providers.base import ProviderError
from app.safe_planner.service import CandidateStep, PlanCandidate
from app.work_intelligence import bind_strategy_execution, create_strategy


GOAL_TEXT = "Add multiplication support to the calculator module with verified tests."


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class FailingProvider:
    provider_name = "fake-local"
    model = "planner-v2"

    async def propose(self, *_args, **_kwargs):
        raise ProviderError("quota exhausted", category="rate_limited")


def _foundation(db, tmp_path, *, tied=False, approved=True):
    owner = User(
        email=f"supervisor-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    goal = MainAIGoal(
        owner_id=owner.id,
        title="calculator multiplication",
        original_instruction=GOAL_TEXT,
        status=MainAIGoalStatus.running,
        current_plan_version=1,
        risk_level="low",
        approval_policy="autonomous_development_work",
        created_by="founder",
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        status=MainAIPlanStatus.active,
        rationale="two bounded verified jobs",
        created_by="founder",
    )
    db.add(plan)
    db.flush()
    implementation = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="add multiplication helper",
        task_type="repo_edit",
        status=MainAITaskStatus.ready,
        priority=10,
        risk_level="low",
        verification_plan=[{"kind": "static_analysis"}],
    )
    focused_test = MainAITask(
        owner_id=owner.id,
        goal_id=goal.id,
        plan_id=plan.id,
        description="add and run focused multiplication test",
        task_type="repo_edit",
        status=MainAITaskStatus.ready if tied else MainAITaskStatus.pending,
        priority=10 if tied else 5,
        risk_level="low",
        verification_plan=[{"kind": "targeted_tests", "target": "test_calculator.py"}],
    )
    db.add_all([implementation, focused_test])
    db.flush()
    if approved:
        grant_task_approval(db, task=implementation, approved_by="founder")
        grant_task_approval(db, task=focused_test, approved_by="founder")
    if not tied:
        db.add(
            MainAITaskDependency(
                owner_id=owner.id,
                task_id=focused_test.id,
                depends_on_task_id=implementation.id,
            )
        )
        db.flush()

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "supervisor-work")
    _git(repo, "config", "user.email", "supervisor@example.com")
    _git(repo, "config", "user.name", "Supervisor Test")
    original = "def add(left, right):\n    return left + right\n"
    (repo / "calculator.py").write_text(original)
    _git(repo, "add", "calculator.py")
    _git(repo, "commit", "-q", "-m", "base")
    strategy = create_strategy(
        db,
        owner_id=owner.id,
        strategy_key=f"scoped-supervisor-{goal.id}",
        version=1,
        work_category="repository_work",
        idempotency_key=f"supervisor-strategy-{goal.id}",
    )

    def prepare_context(task, job):
        job.status = MainAIJobStatus.running
        job.locked_by = "supervisor-worker"
        job.lease_generation = max(job.lease_generation, 1)
        job.started_at = job.started_at or datetime.utcnow()
        job.last_heartbeat_at = datetime.utcnow()
        job.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
        execution = record_execution(
            db,
            owner_id=owner.id,
            task_id=task.id,
            job_id=job.id,
            provider=None,
            model=None,
            idempotency_key=f"supervisor-execution-{task.id}-{job.id}",
        )
        binding = bind_strategy_execution(
            db,
            owner_id=owner.id,
            strategy_id=strategy.id,
            execution_id=execution.id,
            idempotency_key=f"supervisor-binding-{task.id}-{job.id}",
        )
        token = hashlib.sha256(str(task.id).encode()).hexdigest()[:32]
        marker = {"task_id": str(task.id), "job_id": str(job.id), "marker_token": token}
        (repo / ".mainai_worktree_owner.json").write_text(json.dumps(marker))
        exclude = repo / ".git" / "info" / "exclude"
        exclude.write_text(".mainai_worktree_owner.json\n")
        worktree = MainAITaskWorktree(
            task_id=task.id,
            owner_id=owner.id,
            job_id=job.id,
            lease_generation=job.lease_generation,
            executor_id="supervisor-worker",
            repo="local/supervisor-test",
            base_sha=_git(repo, "rev-parse", "HEAD"),
            branch="supervisor-work",
            path=str(repo),
            marker_token=token,
        )
        db.add(worktree)
        db.flush()
        return OperatorContext(
            owner_id=owner.id,
            task_id=task.id,
            job_id=job.id,
            worker_id="supervisor-worker",
            lease_generation=job.lease_generation,
            repository_root=repo,
            expected_base_sha=worktree.base_sha,
            expected_branch="supervisor-work",
            strategy_execution_id=binding.id,
            worktree_id=worktree.id,
            allowed_paths=("calculator.py", "test_calculator.py"),
        )

    scope = SupervisorScope(
        owner_id=owner.id,
        goal_id=goal.id,
        authority_kind="founder_requirement",
        authority_ref=f"mainai_goal:{goal.id}",
        authorized_instruction_sha256=instruction_sha256(GOAL_TEXT),
        repository_identity=str(repo.resolve()),
        allowed_paths=("calculator.py", "test_calculator.py"),
        allowed_capabilities=(
            "read_file",
            "patch_file",
            "create_file",
            "run_static_check",
            "run_focused_test",
            "stage_scoped_changes",
            "commit_scoped_changes",
        ),
        completion_criteria=("multiplication helper and focused test are verified",),
    )
    return (
        owner,
        goal,
        implementation,
        focused_test,
        repo,
        original,
        prepare_context,
        scope,
    )


def _implementation_candidate(original):
    updated = original + "\ndef multiply(left, right):\n    return left * right\n"
    return PlanCandidate(
        "implement bounded multiplication helper",
        "calculator helper exists",
        "patch, statically verify, and commit",
        (
            CandidateStep(
                "patch",
                "add helper",
                "source hash",
                "patch_file",
                {
                    "path": "calculator.py",
                    "content": updated,
                    "expected_sha256": hashlib.sha256(original.encode()).hexdigest(),
                },
                required_risk="LOCAL_WRITE",
            ),
            CandidateStep(
                "check",
                "static verification",
                "ruff pass",
                "run_static_check",
                {"profile_name": "ruff_check", "arguments": ["calculator.py"]},
                ("patch",),
                "LOCAL_EXECUTION",
                verification_required=True,
            ),
            CandidateStep(
                "gate",
                "verify evidence",
                "verification pass",
                "verification_evaluate",
                {},
                ("check",),
            ),
            CandidateStep(
                "stage",
                "stage helper",
                "staged diff",
                "stage_scoped_changes",
                {"paths": ["calculator.py"]},
                ("gate",),
                "LOCAL_WRITE",
            ),
            CandidateStep(
                "commit",
                "commit helper",
                "commit sha",
                "commit_scoped_changes",
                {"message": "Add multiply helper"},
                ("stage",),
                "LOCAL_WRITE",
            ),
        ),
    )


def _test_candidate():
    content = "from calculator import multiply\n\ndef test_multiply():\n    assert multiply(6, 7) == 42\n"
    return PlanCandidate(
        "add focused test",
        "verified multiplication",
        "test and commit",
        (
            CandidateStep(
                "create",
                "add test",
                "test hash",
                "create_file",
                {
                    "path": "test_calculator.py",
                    "content": content,
                    "expected_sha256": None,
                },
                required_risk="LOCAL_WRITE",
            ),
            CandidateStep(
                "test",
                "run focused test",
                "pytest pass",
                "run_focused_test",
                {"profile_name": "focused_pytest", "arguments": ["test_calculator.py"]},
                ("create",),
                "LOCAL_EXECUTION",
                verification_required=True,
            ),
            CandidateStep(
                "gate",
                "verify evidence",
                "verification pass",
                "verification_evaluate",
                {},
                ("test",),
            ),
            CandidateStep(
                "stage",
                "stage test",
                "staged diff",
                "stage_scoped_changes",
                {"paths": ["test_calculator.py"]},
                ("gate",),
                "LOCAL_WRITE",
            ),
            CandidateStep(
                "commit",
                "commit test",
                "commit sha",
                "commit_scoped_changes",
                {"message": "Add multiply test"},
                ("stage",),
                "LOCAL_WRITE",
            ),
        ),
    )


def _independent_candidate():
    content = (
        "from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return replace(
        _test_candidate(),
        interpretation="add independent focused regression",
        steps=(
            CandidateStep(
                "create",
                "add independent test",
                "test hash",
                "create_file",
                {
                    "path": "test_calculator.py",
                    "content": content,
                    "expected_sha256": None,
                },
                required_risk="LOCAL_WRITE",
            ),
            CandidateStep(
                "test",
                "run independent test",
                "pytest pass",
                "run_focused_test",
                {"profile_name": "focused_pytest", "arguments": ["test_calculator.py"]},
                ("create",),
                "LOCAL_EXECUTION",
                verification_required=True,
            ),
            CandidateStep(
                "gate",
                "verify evidence",
                "verification pass",
                "verification_evaluate",
                {},
                ("test",),
            ),
            CandidateStep(
                "stage",
                "stage test",
                "staged diff",
                "stage_scoped_changes",
                {"paths": ["test_calculator.py"]},
                ("gate",),
                "LOCAL_WRITE",
            ),
            CandidateStep(
                "commit",
                "commit test",
                "commit sha",
                "commit_scoped_changes",
                {"message": "Add independent test"},
                ("stage",),
                "LOCAL_WRITE",
            ),
        ),
    )


def _bindings(implementation, focused_test, prepare, original, scope):
    return (
        WorkBinding(
            implementation.id,
            prepare,
            _implementation_candidate(original),
            required_capabilities=("patch_file", "run_static_check"),
            expected_contribution="implement helper",
            repository_identity=scope.repository_identity,
            allowed_paths=scope.allowed_paths,
        ),
        WorkBinding(
            focused_test.id,
            prepare,
            _test_candidate(),
            required_capabilities=("create_file", "run_focused_test"),
            expected_contribution="verify helper",
            repository_identity=scope.repository_identity,
            allowed_paths=scope.allowed_paths,
        ),
    )


@pytest.mark.asyncio
async def test_two_job_chain_and_interruption_resume_are_canonical(
    superuser_db, tmp_path
):
    owner, goal, first, second, repo, original, prepare, scope = _foundation(
        superuser_db, tmp_path
    )
    bindings = _bindings(first, second, prepare, original, scope)
    interrupted = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=bindings,
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert interrupted.classification == "RUN_BOUND_REACHED"
    assert first.status == MainAITaskStatus.completed
    assert second.status == MainAITaskStatus.ready
    resumed = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=bindings,
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert resumed.classification == "COMPLETE"
    assert resumed.completed_jobs == 2
    assert goal.status == MainAIGoalStatus.completed
    assert "multiply" in (repo / "calculator.py").read_text()
    assert (repo / "test_calculator.py").exists()
    assert (
        len(
            superuser_db.execute(
                select(WorkTraceEvent).where(WorkTraceEvent.owner_id == owner.id)
            )
            .scalars()
            .all()
        )
        >= 8
    )
    assert (
        len(
            superuser_db.execute(
                select(IntelligenceEvidence).where(
                    IntelligenceEvidence.owner_id == owner.id,
                    IntelligenceEvidence.evidence_kind == "supervisor_selection",
                )
            )
            .scalars()
            .all()
        )
        == 2
    )
    assert (
        len(
            superuser_db.execute(
                select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
            )
            .scalars()
            .all()
        )
        > 4
    )


@pytest.mark.asyncio
async def test_authority_scope_correction_and_completion_criteria_fail_closed(
    superuser_db, tmp_path
):
    _, goal, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path
    )
    bindings = _bindings(first, second, prepare, original, scope)
    _, state, _ = validate_scope(
        superuser_db, replace(scope, authority_kind="suggestion")
    )
    assert state == "NEEDS_AUTHORIZATION"
    escaped = await run_supervisor(
        superuser_db,
        scope=replace(scope, repository_identity=str(tmp_path.resolve())),
        bindings=bindings,
    )
    assert escaped.classification == "OUT_OF_SCOPE"
    record_founder_correction(
        superuser_db,
        scope=scope,
        corrected_instruction="Only document calculator behavior; do not edit it.",
    )
    corrected = await run_supervisor(superuser_db, scope=scope, bindings=bindings)
    assert corrected.classification == "AUTHORITY_CHANGED"
    assert first.status == MainAITaskStatus.ready
    goal.original_instruction = GOAL_TEXT
    goal.status = MainAIGoalStatus.running
    first.status = MainAITaskStatus.completed
    second.status = MainAITaskStatus.completed
    first.completed_at = datetime.utcnow()
    second.completed_at = datetime.utcnow()
    unclear = await run_supervisor(
        superuser_db, scope=replace(scope, completion_criteria=()), bindings=bindings
    )
    assert unclear.classification == "NEEDS_CLARIFICATION"


@pytest.mark.asyncio
async def test_tie_bounds_cross_owner_and_self_work_have_no_bypass(
    superuser_db, tmp_path
):
    _, _, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    bindings = _bindings(first, second, prepare, original, scope)
    tie = await run_supervisor(superuser_db, scope=scope, bindings=bindings)
    assert tie.classification == "NEEDS_SELECTION"
    with pytest.raises(SupervisorError, match="bound"):
        discover_candidates(
            superuser_db,
            scope=scope,
            bindings=bindings,
            bounds=SupervisorBounds(max_candidates=1),
        )
    with pytest.raises(SupervisorError, match="another owner"):
        await run_supervisor(
            superuser_db, scope=replace(scope, owner_id=uuid.uuid4()), bindings=bindings
        )
    self_tie = await run_supervisor(
        superuser_db, scope=replace(scope, self_work=True), bindings=bindings
    )
    assert self_tie.classification == "NEEDS_SELECTION"


@pytest.mark.asyncio
async def test_blocked_capability_gap_and_provider_outage_do_not_freeze_independent_work(
    superuser_db, tmp_path
):
    from decimal import Decimal

    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
    from app.provider_spend import authorize_provider_spend

    owner, goal, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    proposal = propose_execution_scope(
        superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key=f"outage-prop-{goal.id}"
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=list(scope.allowed_paths),
        authorized_capabilities=list(scope.allowed_capabilities),
        authorized_risk="low",
        envelope_idempotency_key=f"outage-env-{goal.id}",
    )
    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("2.00"),
        max_requests=5,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"outage-spend-{goal.id}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    scope = replace(scope, provider_spend_authorized=True)
    first.priority = 20
    second.priority = 10
    provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        FailingProvider(),
        provider_likely=True,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    independent = WorkBinding(
        second.id,
        prepare,
        _independent_candidate(),
        required_capabilities=("create_file", "run_focused_test"),
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    outage = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding, independent),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert second.status == MainAITaskStatus.completed
    assert outage.classification == "WAITING_PROVIDER"
    provider_checkpoints = [
        row
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == scope.goal_id)
        ).scalars()
        if row.executor_state.get("phase") == "WAITING_PROVIDER"
    ]
    assert provider_checkpoints


@pytest.mark.asyncio
async def test_capability_gap_is_preserved_while_independent_work_continues(
    superuser_db, tmp_path
):
    _, _, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    first.priority = 20
    second.priority = 10
    missing = PlanCandidate(
        "inspect unsupported binary",
        "parsed model",
        "requires a capability that does not exist",
        (CandidateStep("parse", "parse model", "model data", "parse_unknown_binary"),),
    )
    gap = WorkBinding(
        first.id,
        prepare,
        missing,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    independent = WorkBinding(
        second.id,
        prepare,
        _independent_candidate(),
        required_capabilities=("create_file", "run_focused_test"),
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(gap, independent),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert result.classification == "CAPABILITY_MISSING"
    assert second.status == MainAITaskStatus.completed
    assert first.status == MainAITaskStatus.running
    checkpoints = (
        superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == scope.goal_id)
        )
        .scalars()
        .all()
    )
    assert any(
        row.executor_state.get("phase") == "CAPABILITY_MISSING" for row in checkpoints
    )


@pytest.mark.asyncio
async def test_verification_failure_never_completes_parent(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    unverified = PlanCandidate(
        "attempt",
        "change",
        "missing evidence",
        (CandidateStep("gate", "verify", "pass", "verification_evaluate"),),
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                unverified,
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    # Live gap wiring inserts a repair child and parks the source; with max_jobs=1 the run
    # either stops on VERIFICATION_REQUIRED (if the child is not yet selected) or
    # WAITING_APPROVAL once the auto-derived child is selected. Parent must never complete.
    assert result.classification in {"VERIFICATION_REQUIRED", "WAITING_APPROVAL"}
    if result.classification == "VERIFICATION_REQUIRED":
        assert result.explanation.get("followup") == "verification_repair"
    assert goal.status != MainAIGoalStatus.completed
    assert first.status != MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_out_of_scope_capability_candidate_never_dispatches(
    superuser_db, tmp_path
):
    _, _, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    binding = WorkBinding(
        first.id,
        prepare,
        _implementation_candidate("x"),
        required_capabilities=("production_mutation",),
    )
    with pytest.raises(SupervisorError, match="capability envelope"):
        await run_supervisor(
            superuser_db,
            scope=replace(
                scope,
                allowed_capabilities=scope.allowed_capabilities
                + ("production_mutation",),
            ),
            bindings=(binding,),
        )
    assert first.mainai_job_id is None


@pytest.mark.asyncio
async def test_authorized_self_work_uses_the_same_planner_driver_operator_and_verification(
    superuser_db, tmp_path
):
    owner, goal, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path
    )
    instruction = "Improve deterministic development independence within this disposable repository."
    goal.title = "bounded LifeAI-like self-work"
    goal.original_instruction = instruction
    second.status = MainAITaskStatus.blocked
    self_scope = replace(
        scope,
        self_work=True,
        authorized_instruction_sha256=instruction_sha256(instruction),
    )
    binding = WorkBinding(
        first.id,
        prepare,
        _implementation_candidate(original),
        required_capabilities=("patch_file", "run_static_check"),
        repository_identity=self_scope.repository_identity,
        allowed_paths=self_scope.allowed_paths,
    )
    result = await run_supervisor(
        superuser_db,
        scope=self_scope,
        bindings=(binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "RUN_BOUND_REACHED"
    assert first.status == MainAITaskStatus.completed
    assert first.mainai_job_id is not None
    assert (
        len(
            superuser_db.execute(
                select(WorkTraceEvent).where(WorkTraceEvent.owner_id == owner.id)
            )
            .scalars()
            .all()
        )
        >= 4
    )


# --- P1 approval-default regression coverage (founder review finding) -------------------
#
# A WorkBinding with no deterministic candidate falls through to plan_with_provider() -- a
# real, billed external call. That is a spend decision distinct from approval_policy (which
# only gates the MainAITask's own write/commit), so the founder must opt a scope into it via
# scope.provider_spend_authorized explicitly. _foundation() above never sets it, so it
# defaults closed for every test in this file except where it is asked for.


@pytest.mark.asyncio
async def test_provider_spend_denied_without_scope_authorization(superuser_db, tmp_path):
    _, _, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked
    assert scope.provider_spend_authorized is False
    unauthorized_provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        FailingProvider(),
        provider_likely=True,
        independent=False,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(unauthorized_provider_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"
    assert first.status != MainAITaskStatus.completed
    checkpoints = (
        superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == scope.goal_id)
        )
        .scalars()
        .all()
    )
    assert any(
        row.executor_state.get("phase") == "PROVIDER_SPEND_NOT_AUTHORIZED"
        for row in checkpoints
    )


@pytest.mark.asyncio
async def test_unauthorized_provider_spend_does_not_freeze_independent_deterministic_work(
    superuser_db, tmp_path
):
    _, _, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    first.priority = 20
    second.priority = 10
    assert scope.provider_spend_authorized is False
    unauthorized_provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        FailingProvider(),
        provider_likely=True,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    independent = WorkBinding(
        second.id,
        prepare,
        _independent_candidate(),
        required_capabilities=("create_file", "run_focused_test"),
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(unauthorized_provider_binding, independent),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert second.status == MainAITaskStatus.completed
    assert first.status != MainAITaskStatus.completed
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"
