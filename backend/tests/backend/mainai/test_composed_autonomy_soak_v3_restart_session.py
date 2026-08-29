"""Composed autonomy soak v3 — true restart with fresh Worker AND fresh DB session.

Upgrades #195 / soak v2:

* Worker A + Session A run to mid-goal
* CLOSE Session A; discard Worker A; drop all ORM authority objects
* Worker B + NEW Session B continue from durable IDs only

Invariants:
PROCESS MEMORY != AUTHORITY
ORM SESSION MEMORY != AUTHORITY

Worktree filesystem persistence is durable effect state (allowed).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.mainai_execution.provider_wait_wake import WAITING_PROVIDER_BACKOFF_BASE_SECONDS
from app.models.mainai_execution import MainAIGoal, MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.provider_disclosure import ProviderDisclosureEvent
from app.models.provider_spend import ProviderSpendAuthorization
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from app.providers.base import ProviderError
from app.worker import Worker
from tests.backend.mainai.test_composed_autonomy_milestone import (
    _committed_multiply_candidate_payload,
    _divide_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_soak import (
    _SoakPlanningAdapter,
    _out_of_scope_candidate_payload,
)
from tests.backend.mainai.test_composed_autonomy_soak_v2 import (
    _append_fn_payload,
    _bootstrap_four_task_goal,
    _provider_response,
    _worktree_sources,
)

pytest_plugins = ["tests.backend.mainai.test_composed_autonomy_milestone"]


@pytest.mark.asyncio
async def test_composed_autonomy_soak_v3_fresh_worker_and_fresh_session(
    superuser_db, source_repo, monkeypatch
):
    import app.mainai_execution.provider_wait_wake as wake_module

    bind = superuser_db.get_bind()
    SessionFactory = sessionmaker(bind=bind)
    session_a = SessionFactory()

    user, goal, envelope, tasks = await _bootstrap_four_task_goal(session_a)
    ids = {
        "user_id": user.id,
        "goal_id": goal.id,
        "envelope_id": envelope.id,
        "task_ids": [t.id for t in tasks],
    }
    calculator = (source_repo / "calculator.py").read_text(encoding="utf-8")
    monkeypatch.setattr(wake_module, "WAITING_PROVIDER_BACKOFF_BASE_SECONDS", 0.0)
    assert WAITING_PROVIDER_BACKOFF_BASE_SECONDS == 30.0

    def _divide_builder():
        _, current, test = _worktree_sources()
        assert "multiply" in current
        return _provider_response(_divide_candidate_payload(current, test), "soak-v3-b")

    def _subtract_builder():
        _, current, test = _worktree_sources()
        assert "divide" in current
        return _provider_response(
            _append_fn_payload(
                current,
                test,
                fn_name="subtract",
                fn_body="\ndef subtract(left, right):\n    return left - right\n",
                test_fn="def test_subtract():\n    assert subtract(9, 4) == 5",
                imports=["multiply", "divide", "subtract"],
                commit_message="Add subtract helper",
            ),
            "soak-v3-c",
        )

    def _power_builder():
        _, current, test = _worktree_sources()
        assert "subtract" in current
        return _provider_response(
            _append_fn_payload(
                current,
                test,
                fn_name="power",
                fn_body="\ndef power(left, right):\n    return left ** right\n",
                test_fn="def test_power():\n    assert power(2, 3) == 8",
                imports=["multiply", "divide", "subtract", "power"],
                commit_message="Add power helper",
            ),
            "soak-v3-d",
        )

    adapter = _SoakPlanningAdapter(
        [
            ProviderError(
                "transient soak-v3 outage",
                category="rate_limited",
                provider_request_may_have_left=False,
            ),
            _provider_response(_out_of_scope_candidate_payload(), "soak-v3-deny"),
            _provider_response(
                _committed_multiply_candidate_payload(calculator), "soak-v3-a"
            ),
            _divide_builder,
            _subtract_builder,
            _power_builder,
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

    worker_a = Worker()
    worker_a.worker_id = "soak-v3-worker-a"

    t0 = tasks[0]
    await worker_a._advance_authorized_supervisor_goals(session_a)
    session_a.commit()
    session_a.refresh(t0)
    assert t0.status == MainAITaskStatus.blocked
    assert len(adapter.calls) == 0

    authorize_provider_spend(
        session_a,
        owner_id=ids["user_id"],
        goal_id=ids["goal_id"],
        execution_envelope_id=ids["envelope_id"],
        authorized_by="founder",
        max_cost_usd=Decimal("10.00"),
        max_requests=12,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"soak-v3-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    session_a.commit()

    await worker_a._advance_authorized_supervisor_goals(session_a)
    session_a.commit()
    session_a.refresh(t0)
    assert t0.status == MainAITaskStatus.blocked
    assert t0.next_retry_at is not None
    assert len(adapter.calls) == 1

    worker_a._advance_waiting_provider_backoff(session_a)
    session_a.commit()

    for _ in range(12):
        session_a.refresh(t0)
        if t0.status == MainAITaskStatus.completed:
            break
        if t0.status == MainAITaskStatus.blocked and t0.next_retry_at is not None:
            worker_a._advance_waiting_provider_backoff(session_a)
            session_a.commit()
        await worker_a._advance_authorized_supervisor_goals(session_a)
        worker_a._finalize_mainai_execution_goals(session_a)
        session_a.commit()

    session_a.refresh(t0)
    t1 = session_a.get(MainAITask, ids["task_ids"][1])
    assert t0.status == MainAITaskStatus.completed
    assert t1.status == MainAITaskStatus.ready
    assert not list(
        Path(
            __import__(
                "app.development_supervisor.production_worktree", fromlist=["WORKTREE_ROOT"]
            ).WORKTREE_ROOT
        ).rglob("outside_envelope.py")
    )

    # Drop ALL Session A / Worker A / ORM authority objects. Only durable IDs survive.
    session_a.commit()
    session_a.close()
    del worker_a, session_a, user, goal, envelope, tasks, t0, t1

    session_b = SessionFactory()
    assert session_b is not None
    worker_b = Worker()
    worker_b.worker_id = "soak-v3-worker-b"
    assert worker_b.worker_id != "soak-v3-worker-a"

    goal_b = session_b.get(MainAIGoal, ids["goal_id"])
    assert goal_b is not None
    tasks_b = [session_b.get(MainAITask, tid) for tid in ids["task_ids"]]
    assert all(t is not None for t in tasks_b)
    assert tasks_b[0].status == MainAITaskStatus.completed
    assert tasks_b[1].status == MainAITaskStatus.ready

    for _ in range(10):
        session_b.refresh(goal_b)
        if goal_b.status == MainAIGoalStatus.completed:
            break
        await worker_b._advance_authorized_supervisor_goals(session_b)
        worker_b._finalize_mainai_execution_goals(session_b)
        session_b.commit()

    for task in tasks_b:
        session_b.refresh(task)
        assert task.status == MainAITaskStatus.completed
    session_b.refresh(goal_b)
    assert goal_b.status == MainAIGoalStatus.completed
    assert goal_b.final_outcome is not None

    calc_path, final, _ = _worktree_sources()
    assert all(name in final for name in ("multiply", "divide", "subtract", "power"))
    assert not (calc_path.parent / "outside_envelope.py").exists()
    assert len(adapter.calls) == 6

    auth = session_b.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == ids["user_id"],
            ProviderSpendAuthorization.status == "active",
        )
    ).scalar_one()
    assert auth.spent_requests <= auth.max_requests
    assert auth.reserved_requests == 0
    assert (
        session_b.execute(
            select(ProviderDisclosureEvent).where(
                ProviderDisclosureEvent.owner_id == ids["user_id"]
            )
        )
        .scalars()
        .all()
    )

    calls_before = len(adapter.calls)
    jobs_before = len(
        session_b.execute(select(MainAIJob).where(MainAIJob.owner_id == ids["user_id"]))
        .scalars()
        .all()
    )
    for _ in range(3):
        await worker_b._advance_authorized_supervisor_goals(session_b)
        worker_b._finalize_mainai_execution_goals(session_b)
        session_b.commit()
    assert len(adapter.calls) == calls_before
    assert (
        len(
            session_b.execute(select(MainAIJob).where(MainAIJob.owner_id == ids["user_id"]))
            .scalars()
            .all()
        )
        == jobs_before
    )
    assert [
        g.id
        for g, _ in eligible_authorized_goals(session_b, limit=50)
        if g.owner_id == ids["user_id"]
    ] == []

    session_b.close()
