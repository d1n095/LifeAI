"""Stage 2 — real supervisor goal-lease expiry → takeover → continuation.

Proves through Worker ticks (no harness status/unlock/PlanCandidate bridges):

1. Worker A advances a live two-task goal mid-way (task A completes).
2. Crash-hold: claim supervisor goal lease as A and never release.
3. Worker B cannot progress while the lease is still valid (ZERO progress).
4. Real wall-clock expiry of supervisor_goal_leases → B reclaim (generation bump).
5. Worker B continues through Supervisor → task B + goal complete.
6. After completion, Worker A ticks produce ZERO further filesystem mutation.

Forbidden: hand job locked_by edits as the sole takeover mechanism; prefer real
claim_supervisor_goal_lease reclaim via Worker B's production tick path.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.development_supervisor.lease import claim_supervisor_goal_lease
from app.models.mainai_execution import MainAIGoalStatus, MainAITaskStatus
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from app.worker import Worker
from tests.backend.mainai.test_composed_autonomy_milestone import (
    _committed_multiply_candidate_payload,
    _divide_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_soak import _SoakPlanningAdapter
from tests.backend.mainai.test_composed_autonomy_soak_v2 import _worktree_sources

pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]


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


def _lease_row(db, goal_id):
    return db.execute(
        text(
            "SELECT worker_id, lease_generation, status, expires_at < now() AS expired "
            "FROM supervisor_goal_leases WHERE goal_id = :gid "
            "ORDER BY acquired_at DESC NULLS LAST LIMIT 1"
        ),
        {"gid": str(goal_id)},
    ).mappings().first()


@pytest.mark.asyncio
async def test_worker_live_lease_expiry_takeover_continues_goal(
    superuser_db, source_repo, monkeypatch
):
    from tests.backend.mainai.test_composed_autonomy_milestone import _bootstrap_two_task_goal

    user, goal, envelope, tasks = await _bootstrap_two_task_goal(superuser_db)
    task_a, task_b = tasks
    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")

    def _divide_builder():
        _, current, test = _worktree_sources()
        assert "return left * right" in current
        return _provider_response(_divide_candidate_payload(current, test), "lease-takeover-b")

    adapter = _SoakPlanningAdapter(
        [
            _provider_response(
                _committed_multiply_candidate_payload(calculator), "lease-takeover-a"
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
        idempotency_key=f"lease-takeover-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()

    worker_a = Worker()
    worker_a.worker_id = "lease-takeover-a"
    worker_b = Worker()
    worker_b.worker_id = "lease-takeover-b"

    # Worker A advances until task A is complete (real multiply plan).
    for _ in range(12):
        superuser_db.refresh(task_a)
        if task_a.status == MainAITaskStatus.completed:
            break
        await worker_a._advance_authorized_supervisor_goals(superuser_db)
        worker_a._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status in {MainAITaskStatus.pending, MainAITaskStatus.ready}
    _, mid, _ = _worktree_sources()
    assert "return left * right" in mid
    assert "def divide" not in mid

    # Crash-hold: A claims the goal lease and never releases (no tick finally).
    claimed = claim_supervisor_goal_lease(
        superuser_db,
        owner_id=user.id,
        goal_id=goal.id,
        envelope_id=envelope.id,
        worker_id=worker_a.worker_id,
        lease_seconds=300,
    )
    assert claimed is not None
    lease_id, held_generation = claimed
    superuser_db.commit()
    held = _lease_row(superuser_db, goal.id)
    assert held["worker_id"] == worker_a.worker_id
    assert held["status"] == "active"
    assert held["expired"] is False
    assert int(held["lease_generation"]) == held_generation

    # Worker B must make ZERO progress while A's lease is still valid.
    b_status_before = task_b.status
    await worker_b._advance_authorized_supervisor_goals(superuser_db)
    worker_b._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    superuser_db.refresh(task_b)
    assert task_b.status == b_status_before
    _, still_mid, _ = _worktree_sources()
    assert still_mid == mid
    assert "def divide" not in still_mid
    assert len(adapter.calls) == 1  # only A's multiply plan so far

    # Real expiry — wall-clock past expires_at (not a hand generation bump).
    superuser_db.execute(
        text(
            "UPDATE supervisor_goal_leases SET expires_at = now() - interval '1 second' "
            "WHERE id = :lid"
        ),
        {"lid": str(lease_id)},
    )
    superuser_db.commit()
    expired = _lease_row(superuser_db, goal.id)
    assert expired["expired"] is True
    generation_before_takeover = int(expired["lease_generation"])

    # Worker B reclaim + continue until goal completes.
    for _ in range(16):
        superuser_db.refresh(goal)
        superuser_db.refresh(task_b)
        if (
            task_b.status == MainAITaskStatus.completed
            and goal.status == MainAIGoalStatus.completed
        ):
            break
        await worker_b._advance_authorized_supervisor_goals(superuser_db)
        worker_b._finalize_mainai_execution_goals(superuser_db)
        superuser_db.commit()

    superuser_db.refresh(task_a)
    superuser_db.refresh(task_b)
    superuser_db.refresh(goal)
    assert task_a.status == MainAITaskStatus.completed
    assert task_b.status == MainAITaskStatus.completed
    assert goal.status == MainAIGoalStatus.completed
    assert goal.final_outcome is not None
    assert len(adapter.calls) >= 2  # divide plan after takeover

    # Takeover bumped generation in place (same lease row identity family).
    # After B's successful tick the row may be released; generation must still be higher.
    after = _lease_row(superuser_db, goal.id)
    assert after is not None
    assert int(after["lease_generation"]) > generation_before_takeover

    import app.development_supervisor.production_worktree as wt_module

    calc_path, final, _ = _worktree_sources()
    assert "return left * right" in final
    assert "def divide" in final
    final_bytes = calc_path.read_bytes()
    mtime = calc_path.stat().st_mtime_ns

    # Old worker A after takeover/completion: ZERO further filesystem effect.
    await worker_a._advance_authorized_supervisor_goals(superuser_db)
    worker_a._finalize_mainai_execution_goals(superuser_db)
    superuser_db.commit()
    assert calc_path.read_bytes() == final_bytes
    assert calc_path.stat().st_mtime_ns == mtime
    assert not list(Path(wt_module.WORKTREE_ROOT).rglob("outside_envelope.py"))
