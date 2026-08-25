"""Production-shaped autonomous-task harness for Autonomy Activation Lane (B3).

Exercises the REAL Supervisor / Safe Planner / Operator gates with a deterministic fake
provider. Deliberately does NOT call production_entry (Claude owns adjacent authority
surface; final provider_spend_authorized wire deferred).

Authority composition under test:

    active execution envelope
    + founder provider-spend grant  →  provider_spend_is_live(...) == True
    → SupervisorScope.provider_spend_authorized derived from that live check
    → candidate=None binding falls through to plan_with_provider
    → fake adapter PlanCandidate → Safe Planner ACCEPTED → Driver COMPLETE

Without the spend grant the same binding must stop at PROVIDER_SPEND_NOT_AUTHORIZED —
proving the boolean is not flipped open and repo-write authority alone is insufficient.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.development_supervisor.service import (
    SupervisorBounds,
    WorkBinding,
    run_supervisor,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.models.mainai_execution import MainAICheckpoint, MainAITaskStatus
from app.provider_planning.service import ProviderResponse
from app.provider_spend import authorize_provider_spend, provider_spend_is_live
from tests.backend.mainai.test_provider_assisted_planning import (
    FakePlanningAdapter,
    _candidate_payload,
)
from tests.backend.mainai.test_scoped_development_supervisor import _foundation


def _authorize_envelope(db, owner_id, goal_id, *, paths, capabilities):
    proposal = propose_execution_scope(
        db, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"auto-prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=list(paths),
        authorized_capabilities=list(capabilities),
        authorized_risk="low",
        envelope_idempotency_key=f"auto-env-{uuid.uuid4()}",
    )
    return envelope


@pytest.mark.asyncio
async def test_envelope_alone_still_stops_at_provider_spend_not_authorized(superuser_db, tmp_path):
    owner, goal, first, _, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    envelope = _authorize_envelope(
        superuser_db,
        owner.id,
        goal.id,
        paths=scope.allowed_paths,
        capabilities=scope.allowed_capabilities,
    )
    superuser_db.commit()

    assert provider_spend_is_live(
        superuser_db, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=envelope.id
    ) is False
    scope = replace(scope, provider_spend_authorized=provider_spend_is_live(
        superuser_db, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=envelope.id
    ))
    assert scope.provider_spend_authorized is False

    binding = WorkBinding(
        first.id,
        prepare,
        None,
        FakePlanningAdapter(error=AssertionError("provider must not be called without spend")),
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
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"
    assert first.status != MainAITaskStatus.completed


@pytest.mark.asyncio
async def test_live_spend_grant_opens_provider_planning_through_real_supervisor_gates(
    superuser_db, tmp_path
):
    owner, goal, first, second, _, original, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked
    envelope = _authorize_envelope(
        superuser_db,
        owner.id,
        goal.id,
        paths=scope.allowed_paths,
        capabilities=scope.allowed_capabilities,
    )
    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=5,
        idempotency_key=f"auto-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
        provenance={"lane": "autonomy-activation-e2e"},
    )
    superuser_db.commit()

    spend_live = provider_spend_is_live(
        superuser_db, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=envelope.id
    )
    assert spend_live is True
    # Derive the Supervisor boolean from the live grant — never hardcode True here.
    scope = replace(scope, provider_spend_authorized=spend_live)

    calculator = original
    adapter = FakePlanningAdapter(
        ProviderResponse(
            content=json.dumps(
                {
                    "candidate": _candidate_payload(calculator),
                    "clarification_required": False,
                    "clarification_question": None,
                    "capability_gaps": [],
                    "useful_components": [],
                    "confidence": 0.8,
                }
            ),
            provider="fake-local",
            model="planner-v2",
            model_version="e2e",
            raw_usage={"prompt_tokens": 50, "completion_tokens": 40},
        )
    )
    binding = WorkBinding(
        first.id,
        prepare,
        None,  # no hand-built PlanCandidate — provider path must run
        adapter,
        provider_likely=True,
        independent=False,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        required_capabilities=scope.allowed_capabilities,
    )

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )

    assert first.status == MainAITaskStatus.completed
    assert result.classification == "RUN_BOUND_REACHED"
    assert result.completed_jobs >= 1
    assert len(adapter.calls) == 1
    repo = Path(scope.repository_identity)
    assert "multiply" in (repo / "calculator.py").read_text()
    phases = [
        row.executor_state.get("phase")
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
    ]
    assert "PROVIDER_SPEND_NOT_AUTHORIZED" not in phases
