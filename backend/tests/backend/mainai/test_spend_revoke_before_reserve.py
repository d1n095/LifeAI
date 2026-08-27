"""Spend TOCTOU: revoke after eligibility / before reserve → zero provider invocation.

`scope.provider_spend_authorized=True` frozen at tick start is NOT enough. Reservation
must re-read the live grant; a founder revoke in that window must never call the adapter.
"""

from __future__ import annotations

import pytest

from app.provider_planning.service import plan_with_provider
from app.provider_spend import (
    ProviderSpendError,
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


@pytest.mark.asyncio
async def test_revoke_after_live_eligibility_before_reserve_invokes_zero_provider(
    superuser_db, tmp_path
):
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

    # Founder revokes AFTER eligibility was true (simulates mid-tick revoke).
    revoke_provider_spend(
        superuser_db,
        owner_id=owner.id,
        authorization_id=grant.id,
        reason="stop before reserve",
    )
    superuser_db.flush()
    assert (
        provider_spend_is_live(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            execution_envelope_id=grant.execution_envelope_id,
        )
        is False
    )

    adapter = FakePlanningAdapter(_response(_candidate_payload(calculator)))
    result = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"
    assert adapter.calls == []


def test_reserve_after_revoke_raises_without_usage_row(superuser_db, tmp_path):
    owner, goal, _, _, _, _, _, _ = _provider_scope(superuser_db, tmp_path)
    grant = get_current_provider_spend_authorization(
        superuser_db, owner_id=owner.id, goal_id=goal.id
    )
    assert grant is not None
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id)
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
