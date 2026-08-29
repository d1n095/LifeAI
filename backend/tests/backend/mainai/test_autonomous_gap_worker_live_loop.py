"""Stage 1 — full live autonomous gap/repair loop through Worker → Supervisor.

Founder edges only at bootstrap (goal, envelope, spend, task approvals) and one
explicit founder grant for the repair child's repo_edit WAITING_APPROVAL.

Runtime after start: Worker ticks only.

Forbidden harness bridges:
- hand repair task / WorkBinding / PlanCandidate
- manual task-status or dependency unlock
- test-side record_final_report
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.mainai_execution.approval import grant_task_approval
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.problem_learning import LifeProblem
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from app.worker import Worker
from tests.backend.mainai.test_autonomous_gap_live_integration import _repair_child
from tests.backend.mainai.test_composed_autonomy_milestone import _divide_candidate_payload
from tests.backend.mainai.test_composed_autonomy_soak import _SoakPlanningAdapter
from tests.backend.mainai.test_composed_autonomy_soak_v2 import _worktree_sources

pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]


def _broken_multiply_candidate_payload(calculator: str) -> dict:
    """Defective multiply plan — lands on disk, fails focused verification."""
    # Different byte length from repair recipe so .pyc cannot stale-reuse.
    broken = (
        calculator.rstrip()
        + "\n\ndef multiply(left, right):\n    return left + right + 0\n"
    )
    test = (
        "from calculator import multiply\n\n"
        "def test_multiply():\n"
        "    assert multiply(3, 4) == 12\n"
    )
    return {
        "interpretation": "Add multiplication with a defective formula.",
        "requested_outcome": "Verified multiplication helper.",
        "rationale": "Exercise the live verification-failure → gap → repair path.",
        "facts": [],
        "assumptions": [],
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
                "purpose": "add defective multiply helper",
                "expected_result": "source hash",
                "capability": "patch_file",
                "arguments": {
                    "path": "calculator.py",
                    "content": broken,
                    "expected_sha256": hashlib.sha256(calculator.encode()).hexdigest(),
                },
                "depends_on": ["inspect"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test-file",
                "purpose": "add multiplication test",
                "expected_result": "test hash",
                "capability": "create_file",
                "arguments": {
                    "path": "test_calculator.py",
                    "content": test,
                    "expected_sha256": None,
                },
                "depends_on": ["patch"],
                "required_risk": "LOCAL_WRITE",
            },
            {
                "step_id": "test",
                "purpose": "run multiplication test",
                "expected_result": "pytest pass",
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
        ],
    }


def _provider_response(candidate: dict, version: str) -> ProviderResponse:
    return ProviderResponse(
        content=json.dumps(
            {
                "candidate": candidate,
                "clarification_required": False,
                "clarification_question": None,
                "capability_gaps": [],
                "useful_components": [],
                "confidence": 0.9,
            }
        ),
        provider="fake-local",
        model="planner-v2",
        model_version=version,
        raw_usage={"prompt_tokens": 20, "completion_tokens": 20},
    )


@pytest.mark.asyncio
async def test_worker_live_gap_repair_reverify_unlocks_downstream(
    superuser_db, source_repo, monkeypatch
):
    from tests.backend.mainai.test_composed_autonomy_milestone import _bootstrap_two_task_goal

    user, goal, envelope, tasks = await _bootstrap_two_task_goal(superuser_db)
    task_a, task_b = tasks
    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")

    def _divide_builder():
        _, current, test = _worktree_sources()
        assert "return left * right" in current
        return _provider_response(_divide_candidate_payload(current, test), "gap-live-b")

    adapter = _SoakPlanningAdapter(
        [
            _provider_response(
                _broken_multiply_candidate_payload(calculator), "gap-live-broken"
            ),
            _divide_builder,
        ]
    )

    class _Registry:
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
        _Registry,
    )

    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("10.00"),
        max_requests=12,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"gap-live-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    worker = Worker()
    worker.worker_id = "gap-live-worker"

    # Tick 1: defective plan → verification failure → structured gap + repair child.
    await worker._advance_authorized_supervisor_goals(superuser_db)
    worker._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)

    problem = superuser_db.execute(
        select(LifeProblem).where(LifeProblem.mainai_task_id == task_a.id)
    ).scalar_one_or_none()
    assert problem is not None, "verification failure must create a durable LifeProblem/gap"
    child = _repair_child(superuser_db, goal, task_a)
    assert task_a.status == MainAITaskStatus.blocked
    assert child.status == MainAITaskStatus.ready
    # B stays pending on unmet dependency — not ready/completed until A re-verifies.
    assert task_b.status == MainAITaskStatus.pending

    envelope_paths = set(
        (problem.provenance or {}).get("execution_envelope", {}).get("allowed_paths")
        or []
    )
    assert envelope_paths.issubset({"calculator.py", "test_calculator.py"})
    assert envelope_paths  # narrowed, non-empty

    # Founder edge: repair child's repo_edit needs explicit approval (not a harness cheat).
    # One tick may park on WAITING_APPROVAL classification before the grant sticks.
    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    grant_task_approval(superuser_db, task=child, approved_by="founder")
    superuser_db.commit()

    # Repair executes via deterministic recipe (no hand PlanCandidate); source re-verifies;
    # dependency unlocks; task B continues via provider.
    for _ in range(16):
        superuser_db.refresh(goal)
        superuser_db.refresh(task_a)
        superuser_db.refresh(task_b)
        superuser_db.refresh(child)
        if (
            task_a.status == MainAITaskStatus.completed
            and task_b.status == MainAITaskStatus.completed
            and goal.status == MainAIGoalStatus.completed
        ):
            break
        await worker._advance_authorized_supervisor_goals(superuser_db)
        worker._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    superuser_db.refresh(child)
    superuser_db.refresh(goal)

    assert child.status == MainAITaskStatus.completed
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status == MainAITaskStatus.completed
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None

    import app.development_supervisor.production_worktree as wt_module

    _, final, _ = _worktree_sources()
    assert "return left * right" in final
    assert "def divide" in final
    assert not list(Path(wt_module.WORKTREE_ROOT).rglob("outside_envelope.py"))

    # Defective plan for A + divide plan for B after unlock (repair uses recipe, not provider).
    assert len(adapter.calls) >= 2
