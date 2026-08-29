"""Phase 8 — production-shaped composed autonomy soak.

Pre-runtime founder edges only (claim → authorize → envelope → approvals → spend).
Runtime = Worker Supervisor + waiting-provider wake + finalize. No hand PlanCandidate,
no manual task status mutation, no test-side record_final_report.

Covers: provider-assisted planning, dependency unlock, one transient provider failure +
wake, one denied out-of-scope plan attempt, successful continuation, goal finalize,
later tick idle. Local Operator effects after ACCEPTED plan (no separate gap-repair
local-only wire in this PR).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.mainai_execution.provider_wait_wake import WAITING_PROVIDER_BACKOFF_BASE_SECONDS
from app.models.mainai_execution import MainAICheckpoint, MainAIGoalStatus, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendUsageEvent
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from app.providers.base import ProviderError
from app.worker import Worker
from tests.backend.mainai.test_composed_autonomy_milestone import (
    _committed_multiply_candidate_payload,
    _divide_candidate_payload,
    _bootstrap_two_task_goal,
)

# Reuse production-shaped worktree/source fixtures from the milestone module.
pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]


def _out_of_scope_candidate_payload() -> dict:
    """Valid envelope shape that cites a path outside the authorized envelope."""
    return {
        "interpretation": "Attempt unauthorized file write.",
        "requested_outcome": "Must be rejected by Safe Planner / scope fence.",
        "rationale": "Adversarial soak: path outside authorized_paths.",
        "facts": [],
        "assumptions": [],
        "unknowns": [],
        "exclusions": [],
        "steps": [
            {
                "step_id": "escape",
                "purpose": "write outside envelope",
                "expected_result": "must never land",
                "capability": "create_file",
                "arguments": {
                    "path": "outside_envelope.py",
                    "content": "ESCAPE\n",
                    "expected_sha256": None,
                },
                "required_risk": "LOCAL_WRITE",
            },
        ],
    }


class _SoakPlanningAdapter:
    """Sequenced fake provider: Exception | ProviderResponse | callable builder."""

    provider_name = "fake-local"
    model = "planner-v2"

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def propose(self, request_payload, *, timeout_seconds, max_output_bytes):
        self.calls.append((request_payload, timeout_seconds, max_output_bytes))
        if not self._script:
            raise AssertionError("unexpected extra provider call")
        item = self._script.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.asyncio
async def test_composed_autonomy_synthetic_soak(
    superuser_db, source_repo, monkeypatch
):
    from app.models.mainai_recovery import MainAITaskWorktree
    import app.mainai_execution.provider_wait_wake as wake_module

    user, goal, envelope, tasks = await _bootstrap_two_task_goal(superuser_db)
    task_a, task_b = tasks
    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")

    # Zero-base backoff so wake is due without mutating task.next_retry_at by hand.
    monkeypatch.setattr(wake_module, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 0.0)
    assert WAITING_PROVIDER_BACKOFF_BASE_SECONDS == 30.0  # production constant unchanged

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
        model_version="soak-a",
        raw_usage={"prompt_tokens": 40, "completion_tokens": 30},
    )
    deny_resp = ProviderResponse(
        content=json.dumps(
            {
                "candidate": _out_of_scope_candidate_payload(),
                "clarification_required": False,
                "clarification_question": None,
                "capability_gaps": [],
                "useful_components": [],
                "confidence": 0.2,
            }
        ),
        provider="fake-local",
        model="planner-v2",
        model_version="soak-deny",
        raw_usage={"prompt_tokens": 20, "completion_tokens": 10},
    )

    def _divide_from_worktree():
        import app.development_supervisor.production_worktree as wt_module

        calc_path = list(Path(wt_module.WORKTREE_ROOT).rglob("calculator.py"))[0]
        test_path = calc_path.parent / "test_calculator.py"
        current = calc_path.read_text(encoding="utf-8")
        existing_test = test_path.read_text(encoding="utf-8")
        assert "multiply" in current
        assert not (calc_path.parent / "outside_envelope.py").exists()
        return ProviderResponse(
            content=json.dumps(
                {
                    "candidate": _divide_candidate_payload(current, existing_test),
                    "clarification_required": False,
                    "clarification_question": None,
                    "capability_gaps": [],
                    "useful_components": [],
                    "confidence": 0.9,
                }
            ),
            provider="fake-local",
            model="planner-v2",
            model_version="soak-b",
            raw_usage={"prompt_tokens": 42, "completion_tokens": 32},
        )

    adapter = _SoakPlanningAdapter(
        [
            ProviderError(
                "transient soak outage",
                category="rate_limited",
                provider_request_may_have_left=False,
            ),
            deny_resp,
            multiply_resp,
            _divide_from_worktree,
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

    worker = Worker()
    worker.worker_id = "composed-soak-worker"

    # Tick 0: no spend → park (zero provider calls).
    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    assert task_a.status == MainAITaskStatus.blocked
    assert len(adapter.calls) == 0

    authorize_provider_spend(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("5.00"),
        max_requests=8,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"soak-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()
    superuser_db.refresh(task_a)
    assert task_a.status == MainAITaskStatus.ready

    # Tick 1: transient rate_limited → WAITING_PROVIDER park.
    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    assert task_a.status == MainAITaskStatus.blocked
    assert task_a.next_retry_at is not None
    assert len(adapter.calls) == 1

    # Wake via Worker clock (no manual status / next_retry_at mutation).
    worker._advance_waiting_provider_backoff(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    assert task_a.status == MainAITaskStatus.ready

    # Tick 2: denied out-of-scope plan — no unauthorized filesystem effect.
    await worker._advance_authorized_supervisor_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    assert len(adapter.calls) == 2
    import app.development_supervisor.production_worktree as wt_module

    wt_root = Path(wt_module.WORKTREE_ROOT)
    assert not list(wt_root.rglob("outside_envelope.py"))
    deny_phases = {
        row.executor_state.get("phase")
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    }
    assert deny_phases & {
        "PROVIDER_FAILED",
        "OUT_OF_SCOPE",
        "REJECTED_UNSUPPORTED",
        "REJECTED_UNSAFE",
        "WAITING_PROVIDER",
    }

    # If deny left the task blocked/waiting, wake again without status cheat.
    superuser_db.refresh(task_a)
    if task_a.status == MainAITaskStatus.blocked and task_a.next_retry_at is not None:
        worker._advance_waiting_provider_backoff(superuser_db)
        superuser_db.commit()
        superuser_db.refresh(task_a)

    # Recover: production entry redispatch / resume until A completes (bounded ticks).
    for _ in range(6):
        superuser_db.refresh(task_a)
        if task_a.status == MainAITaskStatus.completed:
            break
        if task_a.status == MainAITaskStatus.blocked and task_a.next_retry_at is not None:
            worker._advance_waiting_provider_backoff(superuser_db)
            superuser_db.commit()
        await worker._advance_authorized_supervisor_goals(superuser_db)
        worker._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    superuser_db.refresh(goal)
    assert task_a.status == MainAITaskStatus.completed, [
        (row.executor_state.get("phase"), row.executor_state.get("classification"))
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert task_b.status == MainAITaskStatus.ready
    assert goal.status == MainAIGoalStatus.running

    calc_files = list(wt_root.rglob("calculator.py"))
    assert len(calc_files) == 1 and "multiply" in calc_files[0].read_text(encoding="utf-8")
    assert not list(wt_root.rglob("outside_envelope.py"))

    # Task B → complete → goal finalize.
    await worker._advance_authorized_supervisor_goals(superuser_db)
    worker._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    superuser_db.refresh(goal)
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status == MainAITaskStatus.completed
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None

    final = calc_files[0].read_text(encoding="utf-8")
    assert "multiply" in final and "divide" in final
    assert not list(wt_root.rglob("outside_envelope.py"))
    assert (
        superuser_db.execute(
            select(MainAITaskWorktree).where(MainAITaskWorktree.owner_id == user.id)
        )
        .scalars()
        .all()
        == []
    )

    # Spend within authorization; no duplicate settled success beyond expected calls.
    usage = (
        superuser_db.execute(
            select(ProviderSpendUsageEvent).where(
                ProviderSpendUsageEvent.owner_id == user.id
            )
        )
        .scalars()
        .all()
    )
    settled = [u for u in usage if u.status == "settled"]
    assert settled
    auth = superuser_db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == user.id,
            ProviderSpendAuthorization.status == "active",
        )
    ).scalar_one()
    assert auth.spent_requests <= auth.max_requests
    assert auth.reserved_requests == 0
    assert auth.spent_cost_usd <= auth.max_cost_usd

    # Disclosure ledger sane (at least one row for successful planning invokes).
    disclosures = (
        superuser_db.execute(
            select(ProviderDisclosureEvent).where(
                ProviderDisclosureEvent.owner_id == user.id
            )
        )
        .scalars()
        .all()
    )
    assert disclosures

    # Exact provider call count: rate_limit + deny + A + B.
    assert len(adapter.calls) == 4

    # Later tick: zero consequential effect.
    jobs_before = (
        superuser_db.execute(select(MainAIJob).where(MainAIJob.owner_id == user.id))
        .scalars()
        .all()
    )
    calls_before = len(adapter.calls)
    await worker._advance_authorized_supervisor_goals(superuser_db)
    worker._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    assert len(adapter.calls) == calls_before
    superuser_db.refresh(goal)
    assert goal.status == MainAIGoalStatus.completed
    assert [
        g.id
        for g, _ in eligible_authorized_goals(superuser_db, limit=50)
        if g.owner_id == user.id
    ] == []
    jobs_after = (
        superuser_db.execute(select(MainAIJob).where(MainAIJob.owner_id == user.id))
        .scalars()
        .all()
    )
    assert len(jobs_after) == len(jobs_before)
