"""Provider crash windows around reserve → invoke → settle.

A. reserved + (possible) invoke → crash before settle → retry must NOT re-invoke
B. provider failure before effect → release frees budget (existing); no stuck hold
C. settle twice → idempotent single spend
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.provider_spend import (
    ProviderSpendUsageEvent,
    ProviderSpendUsageStatus,
)
from app.provider_planning.service import plan_with_provider
from app.provider_spend import (
    release_provider_spend_call,
    reserve_provider_spend_call,
    settle_provider_spend_call,
)
from app.provider_spend.service import get_current_provider_spend_authorization
from tests.backend.mainai.test_provider_assisted_planning import (
    FakePlanningAdapter,
    _candidate_payload,
    _provider_scope,
    _response,
)


@pytest.mark.asyncio
async def test_crash_after_reserve_before_settle_refuses_second_provider_invoke(
    superuser_db, tmp_path
):
    """Window A: leave usage reserved (simulate crash before settle), re-enter planning.

    Required: zero additional adapter calls; reservation not silently released as unspent;
    truthful settle or honest WAITING without re-invoke.
    """
    owner, goal, task, job, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    adapter = FakePlanningAdapter(_response(_candidate_payload(calculator)))

    # First entry: monkeypatch settle to raise after successful propose (crash window).
    import app.provider_planning.service as planning_mod
    from app.provider_spend import settle_provider_spend_call as real_settle

    settle_calls = {"n": 0}

    def crash_before_settle(*args, **kwargs):
        settle_calls["n"] += 1
        raise RuntimeError("simulated process crash before settle")

    # Use reserve normally but replace settle inside plan_with_provider import path.
    original = None

    async def run_once():
        nonlocal original
        # Patch the name used after local import inside plan_with_provider by wrapping
        # settle at module level that reserve path uses — plan_with_provider imports
        # settle inside the function, so patch app.provider_spend.settle_provider_spend_call.
        import app.provider_spend as spend_mod

        original = spend_mod.settle_provider_spend_call
        spend_mod.settle_provider_spend_call = crash_before_settle
        try:
            return await plan_with_provider(
                superuser_db,
                request=request,
                operator_context=context,
                adapter=adapter,
            )
        finally:
            spend_mod.settle_provider_spend_call = original

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await run_once()

    assert len(adapter.calls) == 1
    assert settle_calls["n"] == 1

    usage = superuser_db.execute(
        select(ProviderSpendUsageEvent).where(ProviderSpendUsageEvent.owner_id == owner.id)
    ).scalars().all()
    assert len(usage) == 1
    assert usage[0].status == ProviderSpendUsageStatus.reserved.value

    # Recovery / retry of same request: must NOT call provider again.
    second = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert len(adapter.calls) == 1, "crash-before-settle retry must not re-invoke provider"
    assert second.classification in {
        "WAITING_PROVIDER",
        "PROVIDER_SPEND_NOT_AUTHORIZED",
        "PROVIDER_FAILED",
    }
    superuser_db.refresh(usage[0])
    # Ambiguous prior call must not be labelled unspent (released without settle).
    assert usage[0].status != ProviderSpendUsageStatus.released.value


def test_settle_twice_is_idempotent_single_spend(superuser_db, tmp_path):
    """Window C."""
    owner, goal, _, _, _, _, _, _ = _provider_scope(superuser_db, tmp_path)
    grant = get_current_provider_spend_authorization(
        superuser_db, owner_id=owner.id, goal_id=goal.id
    )
    assert grant is not None
    source_ref = f"settle-twice-{uuid.uuid4()}"
    reserve_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref=source_ref,
        provider="fake-local",
        model="planner-v2",
    )
    first = settle_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        source_ref=source_ref,
        prompt_tokens=3,
        completion_tokens=2,
        cost_usd="0.02",
    )
    second = settle_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        source_ref=source_ref,
        prompt_tokens=99,
        completion_tokens=99,
        cost_usd="9.99",
    )
    assert first.id == second.id
    assert second.status == ProviderSpendUsageStatus.settled.value
    assert second.prompt_tokens == 3
    assert second.cost_usd == Decimal("0.020000")
    superuser_db.refresh(grant)
    assert grant.spent_requests == 1


def test_release_after_failure_then_same_source_ref_does_not_open_free_invoke(
    superuser_db, tmp_path
):
    """After clean release, same source_ref must fail closed (no free re-hold)."""
    from app.provider_spend import ProviderSpendError

    owner, goal, _, _, _, _, _, _ = _provider_scope(superuser_db, tmp_path)
    source_ref = f"release-retry-{uuid.uuid4()}"
    reserve_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref=source_ref,
        provider="fake-local",
        model="planner-v2",
    )
    release_provider_spend_call(
        superuser_db, owner_id=owner.id, source_ref=source_ref
    )
    with pytest.raises(ProviderSpendError, match="already released"):
        reserve_provider_spend_call(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref=source_ref,
            provider="fake-local",
            model="planner-v2",
        )
