"""Founder cancel after ACCEPTED plan / before Driver consequential effects.

Provider/Safe Planner output remains historical DATA. Supervisor must refuse
entering run_driver when cancel_requested is already authoritative.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.development_driver import service as driver_module
from app.development_supervisor import service as supervisor_module
from app.development_supervisor.service import SupervisorBounds, WorkBinding, run_supervisor
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.models.mainai_execution import MainAICheckpoint, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.work_intelligence import WorkTraceEvent
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend
from tests.backend.mainai.test_composed_autonomy_milestone import _candidate_payload
from tests.backend.mainai.test_scoped_development_supervisor import _foundation


class _AcceptOnceProvider:
    provider_name = "fake-local"
    model = "planner-v2"

    def __init__(self, response: ProviderResponse):
        self._response = response
        self.calls = 0

    async def propose(self, *_a, **_k):
        self.calls += 1
        return self._response


@pytest.mark.asyncio
async def test_cancel_after_accepted_plan_refuses_driver_zero_fs_effect(
    superuser_db, tmp_path, monkeypatch
):
    owner, goal, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked

    proposal = propose_execution_scope(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        idempotency_key=f"cancel-accept-prop-{goal.id}",
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=list(scope.allowed_paths),
        authorized_capabilities=list(scope.allowed_capabilities),
        authorized_risk="low",
        envelope_idempotency_key=f"cancel-accept-env-{goal.id}",
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
        idempotency_key=f"cancel-accept-spend-{goal.id}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    scope = replace(scope, provider_spend_authorized=True)

    provider = _AcceptOnceProvider(
        ProviderResponse(
            content=json.dumps(
                {
                    "candidate": _candidate_payload(original),
                    "clarification_required": False,
                    "clarification_question": None,
                    "capability_gaps": [],
                    "useful_components": [],
                    "confidence": 0.9,
                }
            ),
            provider="fake-local",
            model="planner-v2",
            model_version="cancel-accept",
            raw_usage={"prompt_tokens": 10, "completion_tokens": 10},
        )
    )

    real_plan = supervisor_module.plan_with_provider
    driver_calls: list[object] = []

    async def _plan_then_founder_cancel(db, **kwargs):
        result = await real_plan(db, **kwargs)
        assert result.classification == "ACCEPTED"
        # Deterministic barrier: cancel after ACCEPTED, before Driver handoff.
        job = db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == owner.id)
        ).scalars().first()
        assert job is not None
        job.cancel_requested = True
        db.flush()
        return result

    real_run_driver = driver_module.run_driver

    def _spy_run_driver(*args, **kwargs):
        driver_calls.append((args, kwargs))
        return real_run_driver(*args, **kwargs)

    monkeypatch.setattr(supervisor_module, "plan_with_provider", _plan_then_founder_cancel)
    monkeypatch.setattr(supervisor_module, "run_driver", _spy_run_driver)
    monkeypatch.setattr(driver_module, "run_driver", _spy_run_driver)

    binding = WorkBinding(
        first.id,
        prepare,
        None,
        provider,
        provider_likely=True,
        independent=False,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "CANCELLED"
    assert provider.calls == 1
    assert driver_calls == [], "Driver must not run after cancel between ACCEPTED and effect"
    assert first.status != MainAITaskStatus.completed
    calc = list(tmp_path.rglob("calculator.py"))
    assert calc and "multiply" not in calc[0].read_text(encoding="utf-8")
    assert (
        superuser_db.execute(
            select(WorkTraceEvent).where(WorkTraceEvent.owner_id == owner.id)
        )
        .scalars()
        .all()
        == []
    )
    phases = {
        row.executor_state.get("phase")
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    }
    assert "CANCELLED" in phases
