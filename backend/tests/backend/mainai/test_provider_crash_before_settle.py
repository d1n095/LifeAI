"""Provider crash windows around reserve → invoke → settle.

A. reserved + (possible) invoke → crash before settle → retry must NOT re-invoke
B. ambiguous post-invoke failure → do NOT release; retry must NOT re-invoke
   (proven pre-invoke failure may still release — opt-in via provider_request_may_have_left=False)
C. settle twice → idempotent single spend
Also: concurrent first-reserve on the same source_ref → at most one adapter.propose().
"""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.models.provider_spend import (
    ProviderSpendUsageEvent,
    ProviderSpendUsageStatus,
)
from app.provider_planning.service import plan_with_provider
from app.providers.base import ProviderError
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


class _AmbiguousAfterBoundaryAdapter:
    """Signals that propose() was entered (request may have left), then fails ambiguously."""

    def __init__(self):
        self.calls = []
        self.boundary_crossed = False
        self.provider_name = "fake-local"
        self.model = "planner-v2"

    async def propose(self, request_payload, *, timeout_seconds, max_output_bytes):
        self.calls.append((request_payload, timeout_seconds, max_output_bytes))
        self.boundary_crossed = True
        # Client-side timeout / lost response AFTER the invocation boundary — not a
        # proven pre-invoke failure. Must not be treated as "no external effect".
        raise TimeoutError("client timeout after request may have left the process")


@pytest.mark.asyncio
async def test_ambiguous_exception_after_propose_does_not_release_or_reinvoke(
    superuser_db, tmp_path
):
    """Window B / case C: ambiguous post-boundary failure keeps the reservation held.

    Pre-fix gap (#182 review): any exception around propose() released the hold, so a
    retry allocated a fresh :aN source_ref and could re-invoke despite unknown outcome.
    Post-fix: reservation stays reserved → Window A recovery → zero second propose().
    """
    owner, goal, task, job, _, context, request, _calculator = _provider_scope(
        superuser_db, tmp_path
    )
    adapter = _AmbiguousAfterBoundaryAdapter()

    first = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert first.classification == "WAITING_PROVIDER"
    assert adapter.boundary_crossed is True
    assert len(adapter.calls) == 1
    assert first.explanation.get("spend_released") is False

    usage = superuser_db.execute(
        select(ProviderSpendUsageEvent).where(ProviderSpendUsageEvent.owner_id == owner.id)
    ).scalars().all()
    assert len(usage) == 1
    assert usage[0].status == ProviderSpendUsageStatus.reserved.value

    second = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert len(adapter.calls) == 1, (
        "ambiguous post-invoke failure must not allow a second provider invoke"
    )
    assert second.classification == "WAITING_PROVIDER"
    assert second.explanation.get("failure_category") == "unresolved_reservation"

    superuser_db.refresh(usage[0])
    assert usage[0].status == ProviderSpendUsageStatus.settled.value


@pytest.mark.asyncio
async def test_proven_pre_invoke_failure_still_releases_spend(superuser_db, tmp_path):
    """Case A: explicit provider_request_may_have_left=False may still release."""
    owner, goal, _, _, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    adapter = FakePlanningAdapter(
        error=ProviderError(
            "quota unavailable",
            category="rate_limited",
            provider_request_may_have_left=False,
        )
    )
    first = await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter,
    )
    assert first.classification == "WAITING_PROVIDER"
    assert first.explanation.get("spend_released") is True
    assert len(adapter.calls) == 1

    usage = superuser_db.execute(
        select(ProviderSpendUsageEvent).where(ProviderSpendUsageEvent.owner_id == owner.id)
    ).scalars().all()
    assert len(usage) == 1
    assert usage[0].status == ProviderSpendUsageStatus.released.value

    # After clean release, a later retry may allocate :aN and invoke again.
    adapter2 = FakePlanningAdapter(_response(_candidate_payload(calculator)))
    await plan_with_provider(
        superuser_db,
        request=request,
        operator_context=context,
        adapter=adapter2,
    )
    assert len(adapter2.calls) == 1


def test_concurrent_same_source_ref_only_one_reservation_created(superuser_db, tmp_path):
    """#182 review gap (spend layer): two simultaneous first-time callers, one INSERT.

    Exactly one caller must observe created=True. The planning gate uses that flag to
    refuse a second adapter.propose() without settling the winner's live hold.
    """
    owner, goal, _, _, _, _, _, _ = _provider_scope(superuser_db, tmp_path)
    superuser_db.commit()
    owner_id, goal_id = owner.id, goal.id
    bind = superuser_db.get_bind()
    source_ref = f"concurrent-first-{uuid.uuid4()}"
    barrier = threading.Barrier(2)
    created_flags = []
    errors = []

    def _attempt():
        Session = sessionmaker(bind=bind)
        session = Session()
        try:
            barrier.wait(timeout=5)
            _event, created = reserve_provider_spend_call(
                session,
                owner_id=owner_id,
                goal_id=goal_id,
                source_ref=source_ref,
                provider="fake-local",
                model="planner-v2",
            )
            session.commit()
            created_flags.append(created)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(repr(exc))
        finally:
            session.close()

    t1 = threading.Thread(target=_attempt)
    t2 = threading.Thread(target=_attempt)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    assert sorted(created_flags) == [False, True]


@pytest.mark.asyncio
async def test_non_creator_of_reservation_does_not_invoke_provider(superuser_db, tmp_path):
    """Planning gate: reservation_created=False must yield zero adapter.propose() calls."""
    owner, goal, _, _, _, context, request, calculator = _provider_scope(
        superuser_db, tmp_path
    )
    adapter = FakePlanningAdapter(_response(_candidate_payload(calculator)))

    import app.provider_spend as spend_mod

    real_reserve = spend_mod.reserve_provider_spend_call

    def force_non_creator(*args, **kwargs):
        event, _created = real_reserve(*args, **kwargs)
        # Simulate concurrent loser / non-owner of the INSERT.
        return event, False

    spend_mod.reserve_provider_spend_call = force_non_creator
    try:
        result = await plan_with_provider(
            superuser_db,
            request=request,
            operator_context=context,
            adapter=adapter,
        )
    finally:
        spend_mod.reserve_provider_spend_call = real_reserve

    assert len(adapter.calls) == 0
    assert result.classification == "WAITING_PROVIDER"
    assert result.explanation.get("failure_category") in {
        "concurrent_reservation",
        "reservation_not_owned",
    }
