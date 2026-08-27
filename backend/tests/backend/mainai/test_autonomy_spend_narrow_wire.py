"""Prove spend-from-grant wiring and plan-derived capability enforcement."""

from __future__ import annotations

import subprocess
import uuid
from decimal import Decimal

import pytest

from app.development_driver.service import DevelopmentPlan, DriverStep
from app.development_operator.service import OperatorCapabilityMissing, OperatorContext, _require_capability
from app.development_supervisor.plan_scope_narrowing import (
    PlanScopeNarrowingError,
    narrow_task_scope_from_accepted_development_plan,
)
from app.development_supervisor.production_entry import run_authorized_goal_supervisor_tick
from app.development_supervisor.service import SupervisorResult
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.models.mainai_execution import (
    MainAIGoalStatus,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskStatus,
)
from app.provider_spend import authorize_provider_spend, provider_spend_is_live
import app.development_supervisor.production_entry as entry_module


def test_operator_rejects_capability_outside_context_ceiling(tmp_path):
    ctx = OperatorContext(
        owner_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        worker_id="cap-test",
        lease_generation=1,
        repository_root=tmp_path,
        expected_base_sha="0" * 40,
        expected_branch="main",
        strategy_execution_id=uuid.uuid4(),
        allowed_paths=("calculator.py",),
        allowed_capabilities=("read_file",),
    )
    _require_capability(ctx, "read_file")
    with pytest.raises(OperatorCapabilityMissing):
        _require_capability(ctx, "patch_file")
    # Empty ceiling remains permissive for legacy callers.
    legacy = OperatorContext(
        owner_id=ctx.owner_id,
        task_id=ctx.task_id,
        job_id=ctx.job_id,
        worker_id=ctx.worker_id,
        lease_generation=1,
        repository_root=tmp_path,
        expected_base_sha="0" * 40,
        expected_branch="main",
        strategy_execution_id=ctx.strategy_execution_id,
        allowed_paths=("calculator.py",),
    )
    _require_capability(legacy, "patch_file")


def test_accepted_development_plan_narrowing_intersects_envelope():
    plan = DevelopmentPlan(
        plan_id="narrow-1",
        strategy_execution_id=uuid.uuid4(),
        steps=(
            DriverStep(
                capability="patch_file",
                purpose="edit",
                expected_result="hash",
                arguments={
                    "path": "calculator.py",
                    "content": "x",
                    "expected_sha256": "a" * 64,
                },
            ),
            DriverStep(
                capability="run_focused_test",
                purpose="verify",
                expected_result="pass",
                arguments={
                    "profile_name": "focused_pytest",
                    "arguments": ["test_calculator.py"],
                },
            ),
        ),
    )
    narrowed = narrow_task_scope_from_accepted_development_plan(
        envelope_paths=("calculator.py", "test_calculator.py", "README.md"),
        envelope_capabilities=("patch_file", "run_focused_test", "create_file"),
        plan=plan,
    )
    assert narrowed.allowed_paths == ("calculator.py", "test_calculator.py")
    assert narrowed.allowed_capabilities == ("patch_file", "run_focused_test")
    assert "create_file" not in narrowed.allowed_capabilities

    with pytest.raises(PlanScopeNarrowingError):
        narrow_task_scope_from_accepted_development_plan(
            envelope_paths=("calculator.py",),
            envelope_capabilities=("patch_file",),
            plan=plan,
        )


@pytest.mark.asyncio
async def test_production_entry_spend_flag_follows_live_grant_only(
    superuser_db, make_verified_user, tmp_path, monkeypatch
):
    owner, _ = make_verified_user()
    goal = create_goal(
        superuser_db,
        owner_id=owner.id,
        title="spend wire",
        original_instruction="edit README only",
        created_by="test",
    )
    goal.status = MainAIGoalStatus.running
    plan = MainAIPlan(
        owner_id=owner.id,
        goal_id=goal.id,
        version=1,
        status=MainAIPlanStatus.active,
        rationale="wire",
        created_by="test",
    )
    superuser_db.add(plan)
    superuser_db.flush()
    superuser_db.add(
        MainAITask(
            owner_id=owner.id,
            goal_id=goal.id,
            plan_id=plan.id,
            description="touch readme",
            task_type="repo_edit",
            status=MainAITaskStatus.ready,
            priority=10,
            risk_level="low",
            verification_plan=[{"kind": "static_analysis"}],
        )
    )
    superuser_db.flush()
    proposal = propose_execution_scope(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        idempotency_key=f"wire-prop-{goal.id}",
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"wire-env-{goal.id}",
    )
    superuser_db.commit()

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    monkeypatch.setattr(entry_module, "worker_source_repo_root", lambda: repo)
    monkeypatch.setattr(
        entry_module,
        "ensure_goal_worktree_sync",
        lambda **_k: (repo, head, "main"),
    )

    captured: dict = {}

    async def _capture(db, *, scope, bindings, worker_id, bounds=None):
        captured["spend"] = scope.provider_spend_authorized
        captured["caps"] = scope.allowed_capabilities
        return SupervisorResult(
            "PROVIDER_SPEND_NOT_AUTHORIZED",
            goal.id,
            0,
            (),
            (),
            {"reason": "captured"},
        )

    monkeypatch.setattr(entry_module, "run_supervisor", _capture)

    await run_authorized_goal_supervisor_tick(
        superuser_db, goal=goal, envelope=envelope, worker_id="wire-worker"
    )
    assert captured["spend"] is False
    assert (
        provider_spend_is_live(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            execution_envelope_id=envelope.id,
        )
        is False
    )

    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=2,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"wire-spend-{goal.id}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    await run_authorized_goal_supervisor_tick(
        superuser_db, goal=goal, envelope=envelope, worker_id="wire-worker"
    )
    assert captured["spend"] is True
