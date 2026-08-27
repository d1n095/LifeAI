"""#181: Supervisor fail-fast on stale tick-start spend authorization.

OUTCOME B (Claude negative-control review):
The authoritative final security fence for provider EFFECT already lives inside
`plan_with_provider()` → `reserve_provider_spend_call()` → only then `adapter.propose()`.

#181 does NOT invent that fence. It adds an earlier Supervisor re-read so a stale
`scope.provider_spend_authorized=True` (captured at tick start) fails fast BEFORE
entering `plan_with_provider`.

Negative control (confirmed): the direct `plan_with_provider` revoke tests pass on
pre-#181 code because they exercise the INNER gate only.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.development_supervisor import service as supervisor_service
from app.development_supervisor.service import (
    SupervisorBounds,
    WorkBinding,
    run_supervisor,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.models.mainai_execution import MainAITaskStatus
from app.models.provider_spend import ProviderSpendUsageEvent
from app.provider_planning.service import plan_with_provider
from app.provider_spend import (
    ProviderSpendError,
    authorize_provider_spend,
    provider_spend_is_live,
    reserve_provider_spend_call,
    revoke_provider_spend,
)
from app.provider_spend.service import get_current_provider_spend_authorization
from tests.backend.mainai.test_provider_assisted_planning import (
    FakePlanningAdapter,
    _candidate_payload,
    _provider_scope,
    _response,
)
from tests.backend.mainai.test_scoped_development_supervisor import _foundation


# ---------------------------------------------------------------------------
# INNER gate (authoritative for provider effect) — NOT a proof of #181's delta.
# Kept as a lower-level security regression; these pass on pre-#181 code by design.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_reserve_gate_rejects_revoked_grant_with_zero_adapter_calls(
    superuser_db, tmp_path
):
    """Authoritative fence: reserve_provider_spend_call before adapter.propose."""
    owner, goal, _, _, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    grant = get_current_provider_spend_authorization(
        superuser_db, owner_id=owner.id, goal_id=goal.id
    )
    assert grant is not None
    assert provider_spend_is_live(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=grant.execution_envelope_id,
    )
    revoke_provider_spend(
        superuser_db,
        owner_id=owner.id,
        authorization_id=grant.id,
        reason="stop before reserve",
    )
    superuser_db.flush()

    adapter = FakePlanningAdapter(_response(_candidate_payload(calculator)))
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"
    assert adapter.calls == []


def test_inner_reserve_after_revoke_raises(superuser_db, tmp_path):
    owner, goal, _, _, _, _, _, _ = _provider_scope(superuser_db, tmp_path)
    grant = get_current_provider_spend_authorization(
        superuser_db, owner_id=owner.id, goal_id=goal.id
    )
    assert grant is not None
    revoke_provider_spend(
        superuser_db,
        owner_id=owner.id,
        authorization_id=grant.id,
        reason="stop",
    )
    superuser_db.flush()
    with pytest.raises(ProviderSpendError, match="no live provider spend"):
        reserve_provider_spend_call(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref=f"toctou-revoke-{goal.id}",
            provider="fake-local",
            model="planner-v2",
        )


# ---------------------------------------------------------------------------
# OUTER Supervisor fail-fast — THIS is the #181 semantic delta.
# Pre-#181: stale True → plan_with_provider IS entered (inner gate still rejects).
# #181: stale True → plan_with_provider is NEVER entered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_fail_fast_on_stale_tick_start_spend_skips_plan_with_provider(
    superuser_db, tmp_path, monkeypatch
):
    """Prove #181 delta through REAL run_supervisor path.

    Scenario:
      - founder spend grant exists
      - Supervisor scope freezes provider_spend_authorized=True (tick-start read)
      - founder revokes AFTER that frozen boolean
      - same run_supervisor invocation continues
      - #181 live re-read fails fast
      - plan_with_provider never entered
      - zero reservation / zero adapter call

    Mutation control: on pre-#181 code this assertion `plan_calls == []` FAILS because
    Supervisor trusted the stale boolean and entered plan_with_provider (inner gate then
    rejected). Confirmed by Claude's / local negative-control experiment.
    """
    owner, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    second.status = MainAITaskStatus.blocked

    proposal = propose_execution_scope(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        idempotency_key=f"failfast-prop-{uuid.uuid4()}",
        proposed_paths=list(scope.allowed_paths),
        proposed_capabilities=list(scope.allowed_capabilities),
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=list(scope.allowed_paths),
        authorized_capabilities=list(scope.allowed_capabilities),
        authorized_risk="low",
        envelope_idempotency_key=f"failfast-env-{uuid.uuid4()}",
    )
    grant = authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=5,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"failfast-spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.flush()
    assert provider_spend_is_live(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
    )

    # Tick-start capture (stale after revoke below).
    scope = replace(
        scope,
        authority_kind="authorized_goal",
        authority_ref=str(envelope.id),
        provider_spend_authorized=True,
    )

    revoke_provider_spend(
        superuser_db,
        owner_id=owner.id,
        authorization_id=grant.id,
        reason="revoke after tick-start boolean",
    )
    superuser_db.flush()
    assert (
        provider_spend_is_live(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            execution_envelope_id=envelope.id,
        )
        is False
    )
    # Stale boolean still True — #181 must not trust it.
    assert scope.provider_spend_authorized is True

    plan_calls: list[str] = []
    real_plan = supervisor_service.plan_with_provider

    async def tracking_plan_with_provider(*args, **kwargs):
        plan_calls.append("entered")
        return await real_plan(*args, **kwargs)

    monkeypatch.setattr(
        supervisor_service, "plan_with_provider", tracking_plan_with_provider
    )

    adapter = FakePlanningAdapter(
        error=AssertionError("adapter must not be reached after spend revoke")
    )
    binding = WorkBinding(
        first.id,
        prepare,
        None,
        adapter,
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
    assert "still-live" in (result.explanation.get("reason") or "").lower()
    # THE #181 delta: outer fail-fast — never enter plan_with_provider.
    assert plan_calls == [], (
        "Supervisor must fail-fast on stale spend boolean without entering "
        f"plan_with_provider; got entries={plan_calls}"
    )
    assert adapter.calls == []
    usage = list(
        superuser_db.execute(
            select(ProviderSpendUsageEvent).where(
                ProviderSpendUsageEvent.owner_id == owner.id
            )
        )
        .scalars()
        .all()
    )
    assert usage == []
    assert first.status != MainAITaskStatus.completed
