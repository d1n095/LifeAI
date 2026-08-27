"""First full composed autonomous MainAI development milestone.

Production-shaped chain (no manual bridges):

    source → claim → interpretation → WorkCandidate → founder authorize
    → envelope → MainAIGoal/task → founder task approval (autonomous repo_edit)
    → Worker Supervisor tick → spend park → founder spend grant → wake
    → Worker tick → reserve → fake CI provider → settle
    → Safe Planner ACCEPTED → plan-derived narrowing
    → Driver → Operator capability enforcement → local edit → verify
    → task completed → later Worker tick does not re-execute the completed task

Fake provider response is CI-only. Runtime path around it is real.
remote_write_authorized stays false. No hand PlanCandidate / manual unblock /
direct run_supervisor replacing the production trigger.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, get_goal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import ClaimType, KnowledgeClaim
from app.models.mainai_execution import MainAICheckpoint, MainAITask, MainAITaskStatus
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend, provider_spend_is_live
from app.work_candidates import authorize_work_candidate, list_unreviewed_work_candidates
from app.worker import Worker
from tests.backend.mainai.test_provider_assisted_planning import (
    FakePlanningAdapter,
    _candidate_payload,
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_worktree_root(tmp_path, monkeypatch):
    import app.development_supervisor.production_worktree as module

    monkeypatch.setattr(module, "WORKTREE_ROOT", tmp_path / "supervisor-goal-worktrees")


@pytest.fixture
def source_repo(tmp_path, monkeypatch):
    import app.development_supervisor.production_entry as entry_module

    repo = tmp_path / "worker-source-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@test.local")
    _git(repo, "config", "user.name", "Worker")
    (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", "calculator.py")
    _git(repo, "commit", "-q", "-m", "seed")
    monkeypatch.setattr(entry_module, "worker_source_repo_root", lambda: repo)
    return repo


_OPERATOR_CAPABILITIES = [
    "read_file",
    "patch_file",
    "create_file",
    "run_focused_test",
    "run_static_check",
    "stage_scoped_changes",
    "commit_scoped_changes",
]


def _install_fake_registry_adapter(monkeypatch, adapter: FakePlanningAdapter):
    class _FakeRegistry:
        provider_name = "fake-local"
        model = "planner-v2"
        default_provider = "fake-local"
        default_model = "planner-v2"

        def __init__(self, db, *, provider_name=None, model=None):
            self.db = db

        async def propose(self, *args, **kwargs):
            return await adapter.propose(*args, **kwargs)

    monkeypatch.setattr(
        "app.provider_planning.service.RegistryPlanningAdapter",
        _FakeRegistry,
    )


async def _bootstrap_authorized_goal(db):
    user = User(
        email=f"milestone-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(user)
    db.flush()
    document = Document(
        title="Källa",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=user.id,
        source_id=document.id,
        claim_text="Lägg till en lokal multiply-hjälpare i calculator.py och verifiera den.",
        extraction_version="v1",
        claim_type=ClaimType.decision,
    )
    db.add(claim)
    db.flush()
    db.commit()

    proposal = record_interpretation_proposal(
        db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"milestone-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    db.commit()
    promote_interpretation_proposal(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Lokal multiply-hjälpare i calculator.py",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"milestone-entity-{uuid.uuid4()}",
    )
    db.commit()

    candidates = list_unreviewed_work_candidates(db, owner_id=user.id)
    assert len(candidates) == 1
    authorized_wc, goal = authorize_work_candidate(
        db,
        owner_id=user.id,
        candidate_id=candidates[0].id,
        authorized_by="founder",
        approval_policy="autonomous_development_work",
    )
    db.commit()
    assert authorized_wc.authorized_goal_id == goal.id
    assert get_goal(db, goal.id).approval_policy == "autonomous_development_work"

    planner.create_plan(
        db,
        goal=goal,
        rationale="single bounded local edit",
        tasks=[
            PlannedTaskSpec(
                description="add multiply helper and focused test",
                task_type="repo_edit",
                risk_level="low",
            )
        ],
        created_by="founder",
    )
    task = db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    # Founder task approval is a real authority edge for autonomous repo_edit — not a cheat.
    grant_task_approval(db, task=task, approved_by="founder")
    proposal_scope = propose_execution_scope(
        db,
        owner_id=user.id,
        goal_id=goal.id,
        idempotency_key=f"milestone-scope-{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal_scope.id,
        authorized_by="founder",
        authorized_paths=["calculator.py", "test_calculator.py"],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"milestone-env-{uuid.uuid4()}",
    )
    db.commit()
    return user, goal, envelope, task


@pytest.mark.asyncio
async def test_composed_autonomous_milestone_grant_plan_execute_verify_continue(
    superuser_db, source_repo, monkeypatch
):
    user, goal, envelope, task = await _bootstrap_authorized_goal(superuser_db)
    assert eligible_authorized_goals(superuser_db, limit=50)
    assert (
        provider_spend_is_live(
            superuser_db,
            owner_id=user.id,
            goal_id=goal.id,
            execution_envelope_id=envelope.id,
        )
        is False
    )

    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")
    adapter = FakePlanningAdapter(
        ProviderResponse(
            content=json.dumps(
                {
                    "candidate": _candidate_payload(calculator),
                    "clarification_required": False,
                    "clarification_question": None,
                    "capability_gaps": [],
                    "useful_components": [],
                    "confidence": 0.9,
                }
            ),
            provider="fake-local",
            model="planner-v2",
            model_version="milestone",
            raw_usage={"prompt_tokens": 40, "completion_tokens": 30},
        )
    )
    _install_fake_registry_adapter(monkeypatch, adapter)

    captured_ceilings: list[tuple[str, ...]] = []
    import app.development_driver.service as driver_module
    import app.development_supervisor.service as supervisor_module

    real_run_driver = driver_module.run_driver

    def _spy_run_driver(db, *, context, plan):
        captured_ceilings.append(tuple(context.allowed_capabilities or ()))
        assert context.allowed_capabilities, (
            "governed Driver path must not inherit legacy empty capability ceiling"
        )
        assert context.remote_write_authorized is False
        return real_run_driver(db, context=context, plan=plan)

    monkeypatch.setattr(driver_module, "run_driver", _spy_run_driver)
    monkeypatch.setattr(supervisor_module, "run_driver", _spy_run_driver)

    worker = Worker()
    worker.worker_id = "composed-milestone-worker"

    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.blocked
    assert len(adapter.calls) == 0

    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("2.00"),
        max_requests=5,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"milestone-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.ready
    assert provider_spend_is_live(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
    )

    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.completed, [
        (row.executor_state.get("phase"), row.executor_state.get("classification"), row.executor_state)
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert len(adapter.calls) == 1
    assert captured_ceilings and all(caps for caps in captured_ceilings)
    assert {"read_file", "patch_file", "create_file", "run_focused_test"} <= set(
        captured_ceilings[0]
    )

    import app.development_supervisor.production_worktree as wt_module

    edited = list(Path(wt_module.WORKTREE_ROOT).rglob("calculator.py"))
    assert edited and "multiply" in edited[0].read_text(encoding="utf-8")
    goal_wt = edited[0].parent
    assert not (goal_wt / ".mainai_worktree_owner.json").exists(), (
        "shared PER-GOAL worktree must not carry PER-JOB ownership markers"
    )
    from app.models.mainai_recovery import MainAITaskWorktree

    assert (
        superuser_db.execute(
            select(MainAITaskWorktree).where(MainAITaskWorktree.owner_id == user.id)
        )
        .scalars()
        .all()
        == []
    )

    usage = (
        superuser_db.execute(
            select(ProviderSpendUsageEvent).where(
                ProviderSpendUsageEvent.owner_id == user.id
            )
        )
        .scalars()
        .all()
    )
    assert len(usage) == 1 and usage[0].status == "settled"
    auth = superuser_db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == user.id,
            ProviderSpendAuthorization.status == "active",
        )
    ).scalar_one()
    assert auth.spent_requests == 1 and auth.reserved_requests == 0

    phases = [
        row.executor_state.get("phase")
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert "PROVIDER_SPEND_NOT_AUTHORIZED" in phases

    calls_before = len(adapter.calls)
    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task)
    assert task.status == MainAITaskStatus.completed
    assert len(adapter.calls) == calls_before


def _committed_multiply_candidate_payload(calculator: str) -> dict:
    """Same as _candidate_payload plus stage/commit so a later same-goal task can reuse HEAD."""
    base = _candidate_payload(calculator)
    steps = list(base["steps"])
    steps.extend(
        [
            {
                "step_id": "stage",
                "purpose": "stage helper and test",
                "expected_result": "staged diff",
                "capability": "stage_scoped_changes",
                "arguments": {"paths": ["calculator.py", "test_calculator.py"]},
                "depends_on": ["gate"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "commit",
                "purpose": "commit helper",
                "expected_result": "commit sha",
                "capability": "commit_scoped_changes",
                "arguments": {"message": "Add multiply helper"},
                "depends_on": ["stage"],
                "required_risk": "LOCAL_WRITE",
            },
        ]
    )
    base["steps"] = steps
    return base


def _divide_candidate_payload(calculator: str, existing_test: str) -> dict:
    import hashlib

    updated = calculator + "\ndef divide(left, right):\n    return left / right\n"
    test = (
        "from calculator import multiply, divide\n\n"
        "def test_multiply():\n"
        "    assert multiply(6, 7) == 42\n\n"
        "def test_divide():\n"
        "    assert divide(10, 2) == 5\n"
    )
    return {
        "interpretation": "Add division helper beside multiply and verify both.",
        "requested_outcome": "A focused, verified division helper.",
        "rationale": "Inspect current calculator, patch exact hash, update focused test.",
        "facts": ["calculator.py already has multiply from the prior task."],
        "assumptions": ["Exact floating division is acceptable."],
        "unknowns": [],
        "exclusions": ["No unrelated edits or remote operations."],
        "steps": [
            {
                "step_id": "inspect",
                "purpose": "inspect current calculator",
                "expected_result": "bounded source text",
                "capability": "read_file",
                "arguments": {"path": "calculator.py"},
            },
            {
                "step_id": "patch",
                "purpose": "add division helper",
                "expected_result": "new source hash",
                "capability": "patch_file",
                "arguments": {
                    "path": "calculator.py",
                    "content": updated,
                    "expected_sha256": hashlib.sha256(calculator.encode()).hexdigest(),
                },
                "depends_on": ["inspect"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test-file",
                "purpose": "update focused test for divide",
                "expected_result": "test file hash",
                "capability": "patch_file",
                "arguments": {
                    "path": "test_calculator.py",
                    "content": test,
                    "expected_sha256": hashlib.sha256(
                        existing_test.encode()
                    ).hexdigest(),
                },
                "depends_on": ["patch"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test",
                "purpose": "verify helpers",
                "expected_result": "pytest exit zero",
                "capability": "run_focused_test",
                "arguments": {
                    "profile_name": "focused_pytest",
                    "arguments": ["test_calculator.py"],
                },
                "depends_on": ["test-file"],
                "required_risk": "LOCAL_EXECUTION",
                "verification_required": True,
            },
            {
                "step_id": "gate",
                "purpose": "evaluate deterministic evidence",
                "expected_result": "verification checkpoint",
                "capability": "verification_evaluate",
                "arguments": {},
                "depends_on": ["test"],
            },
            {
                "step_id": "stage",
                "purpose": "stage helper and test",
                "expected_result": "staged diff",
                "capability": "stage_scoped_changes",
                "arguments": {"paths": ["calculator.py", "test_calculator.py"]},
                "depends_on": ["gate"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "commit",
                "purpose": "commit divide helper",
                "expected_result": "commit sha",
                "capability": "commit_scoped_changes",
                "arguments": {"message": "Add divide helper"},
                "depends_on": ["stage"],
                "required_risk": "LOCAL_WRITE",
            },
        ],
    }


class _SequencedPlanningAdapter:
    """CI-only fake provider; runtime path around it remains real."""

    provider_name = "fake-local"
    model = "planner-v2"

    def __init__(self, responses: list[ProviderResponse]):
        self._responses = list(responses)
        self.calls = []

    async def propose(self, request_payload, *, timeout_seconds, max_output_bytes):
        self.calls.append((request_payload, timeout_seconds, max_output_bytes))
        if not self._responses:
            raise AssertionError("unexpected extra provider call")
        return self._responses.pop(0)


async def _bootstrap_two_task_goal(db):
    user = User(
        email=f"two-task-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(user)
    db.flush()
    document = Document(
        title="Källa",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=user.id,
        source_id=document.id,
        claim_text=(
            "Lägg till multiply och sedan divide i calculator.py, i två sekventiella tasks."
        ),
        extraction_version="v1",
        claim_type=ClaimType.decision,
    )
    db.add(claim)
    db.flush()
    db.commit()

    proposal = record_interpretation_proposal(
        db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"two-task-proposal-{uuid.uuid4()}",
        classifier_strategy="claim_type_extraction_v1",
        classifier_confidence="certain",
    )
    db.commit()
    promote_interpretation_proposal(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        entity_type="decision",
        title="Lokal multiply sedan divide i calculator.py",
        authority="founder",
        basis="manual",
        entity_idempotency_key=f"two-task-entity-{uuid.uuid4()}",
    )
    db.commit()

    candidates = list_unreviewed_work_candidates(db, owner_id=user.id)
    assert len(candidates) == 1
    _, goal = authorize_work_candidate(
        db,
        owner_id=user.id,
        candidate_id=candidates[0].id,
        authorized_by="founder",
        approval_policy="autonomous_development_work",
    )
    db.commit()

    planner.create_plan(
        db,
        goal=goal,
        rationale="two sequential local edits on shared goal worktree",
        tasks=[
            PlannedTaskSpec(
                description="add multiply helper and focused test",
                task_type="repo_edit",
                risk_level="low",
            ),
            PlannedTaskSpec(
                description="add divide helper and update focused test",
                task_type="repo_edit",
                risk_level="low",
                depends_on=[0],
            ),
        ],
        created_by="founder",
    )
    tasks = (
        db.execute(
            select(MainAITask)
            .where(MainAITask.goal_id == goal.id)
            .order_by(MainAITask.created_at.asc(), MainAITask.id.asc())
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 2
    for task in tasks:
        grant_task_approval(db, task=task, approved_by="founder")
    proposal_scope = propose_execution_scope(
        db,
        owner_id=user.id,
        goal_id=goal.id,
        idempotency_key=f"two-task-scope-{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal_scope.id,
        authorized_by="founder",
        authorized_paths=["calculator.py", "test_calculator.py"],
        authorized_capabilities=_OPERATOR_CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"two-task-env-{uuid.uuid4()}",
    )
    db.commit()
    return user, goal, envelope, tasks


@pytest.mark.asyncio
async def test_composed_two_task_same_goal_worktree_continuation(
    superuser_db, source_repo, monkeypatch
):
    """Essential multi-task proof: shared PER-GOAL worktree reused without ownership false-positives."""
    from app.models.mainai_execution import MainAIGoalStatus
    from app.models.mainai_recovery import MainAITaskWorktree

    user, goal, envelope, tasks = await _bootstrap_two_task_goal(superuser_db)
    task_a, task_b = tasks

    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")
    multiply_resp = ProviderResponse(
        content=json.dumps(
            {
                "candidate": _committed_multiply_candidate_payload(calculator),
                "clarification_required": False,
                "clarification_question": None,
                "capability_gaps": [],
                "useful_components": [],
                "confidence": 0.9,
            }
        ),
        provider="fake-local",
        model="planner-v2",
        model_version="two-task-a",
        raw_usage={"prompt_tokens": 40, "completion_tokens": 30},
    )
    # Task B payload is built after A lands; adapter rebuilds from on-disk worktree at call time.
    adapter = _SequencedPlanningAdapter([multiply_resp])

    class _DynamicRegistry:
        provider_name = "fake-local"
        model = "planner-v2"
        default_provider = "fake-local"
        default_model = "planner-v2"

        def __init__(self, db, *, provider_name=None, model=None):
            self.db = db

        async def propose(self, *args, **kwargs):
            if len(adapter.calls) >= 1 and not adapter._responses:
                import app.development_supervisor.production_worktree as wt_module

                calc_path = list(Path(wt_module.WORKTREE_ROOT).rglob("calculator.py"))[0]
                test_path = calc_path.parent / "test_calculator.py"
                current = calc_path.read_text(encoding="utf-8")
                existing_test = test_path.read_text(encoding="utf-8")
                assert "multiply" in current
                adapter._responses.append(
                    ProviderResponse(
                        content=json.dumps(
                            {
                                "candidate": _divide_candidate_payload(
                                    current, existing_test
                                ),
                                "clarification_required": False,
                                "clarification_question": None,
                                "capability_gaps": [],
                                "useful_components": [],
                                "confidence": 0.9,
                            }
                        ),
                        provider="fake-local",
                        model="planner-v2",
                        model_version="two-task-b",
                        raw_usage={"prompt_tokens": 42, "completion_tokens": 32},
                    )
                )
            return await adapter.propose(*args, **kwargs)

    monkeypatch.setattr(
        "app.provider_planning.service.RegistryPlanningAdapter",
        _DynamicRegistry,
    )

    worker = Worker()
    worker.worker_id = "composed-two-task-worker"

    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    assert task_a.status == MainAITaskStatus.blocked

    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("5.00"),
        max_requests=5,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"two-task-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status == MainAITaskStatus.ready
    assert len(adapter.calls) == 1
    # Invariant A: first task complete while dependent remains ready → goal stays running.
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.running
    assert goal.final_outcome is None

    import app.development_supervisor.production_worktree as wt_module

    goal_files = list(Path(wt_module.WORKTREE_ROOT).rglob("calculator.py"))
    assert len(goal_files) == 1
    assert "multiply" in goal_files[0].read_text(encoding="utf-8")
    assert not (goal_files[0].parent / ".mainai_worktree_owner.json").exists()

    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    superuser_db.refresh(goal)
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status == MainAITaskStatus.completed, [
        (row.executor_state.get("phase"), row.executor_state.get("classification"))
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert len(adapter.calls) == 2
    # Canonical B5 rollup: Driver completion gate → recompute → record_final_report.
    # No manual goal mutation / no test-side record_final_report bridge.
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert goal.completed_at is not None

    final = goal_files[0].read_text(encoding="utf-8")
    assert "multiply" in final and "divide" in final
    assert not (goal_files[0].parent / ".mainai_worktree_owner.json").exists()
    assert (
        superuser_db.execute(
            select(MainAITaskWorktree).where(MainAITaskWorktree.owner_id == user.id)
        )
        .scalars()
        .all()
        == []
    )

    # Later Worker tick: zero provider calls, no re-execution, goal stays completed.
    from app.development_supervisor.production_entry import eligible_authorized_goals
    from app.models.mainai_job import MainAIJob

    jobs_before = (
        superuser_db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == user.id)
        )
        .scalars()
        .all()
    )
    calls_before = len(adapter.calls)
    await worker._advance_authorized_supervisor_goals(superuser_db)
    worker._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    assert len(adapter.calls) == calls_before
    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    superuser_db.refresh(goal)
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status == MainAITaskStatus.completed
    assert goal.status == MainAIGoalStatus.completed
    assert [
        g.id for g, _ in eligible_authorized_goals(superuser_db, limit=50) if g.owner_id == user.id
    ] == []
    jobs_after = (
        superuser_db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == user.id)
        )
        .scalars()
        .all()
    )
    assert len(jobs_after) == len(jobs_before)