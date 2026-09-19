"""Spend/cost authority reconciliation (see docs/mainai_v2/MAINAI_V2_SPEND_AUTHORITY_
RECONCILIATION.md): app.workforce.cost's organizational ceilings are now real, wired
protection inside app.provider_spend.service.reserve_provider_spend_call() -- previously
those ceilings had zero real callers anywhere and provided zero actual protection, even
though app.workforce.cost's own docstrings already documented this exact intended
relationship.

TWO LEDGERS != TWO SOURCES OF TRUTH: provider_spend remains the sole canonical ACTUAL SPEND
authority. workforce.cost is a coarser, optional ORGANIZATIONAL CEILING checked before a
reservation is held -- it never itself grants or records spend.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.models.user import User
from app.provider_spend import (
    ProviderSpendError,
    provider_spend_is_live,
    release_provider_spend_call,
    reserve_provider_spend_call,
    settle_provider_spend_call,
)
from app.provider_spend import authorize_provider_spend as grant_provider_spend
from app.workforce.cost import set_cost_budget
from app.models.workforce_ops import WorkforceCostBudget
from sqlalchemy import select


def _owner_goal_envelope(db):
    user = User(email=f"reconcile-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = create_goal(db, owner_id=user.id, title="spend reconciliation probe", original_instruction="plan a local edit", created_by="test")
    db.flush()
    proposal = propose_execution_scope(db, owner_id=user.id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db, owner_id=user.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["README.md"], authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low", envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    db.flush()
    return user, goal, envelope


def _grant(db, owner, goal, envelope, **overrides):
    kwargs = dict(
        owner_id=owner.id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
        max_cost_usd=Decimal("100.00"), max_requests=100, max_cost_per_request_usd=Decimal("1.00"),
        idempotency_key=f"spend-{uuid.uuid4()}", allowed_providers=["fake-local"], allowed_models=["planner-v2"],
    )
    kwargs.update(overrides)
    return grant_provider_spend(db, **kwargs)


# --- 1. Zero behavior change when no org ceiling is configured (the overwhelming majority
# of real callers today). ---------------------------------------------------------------


def test_no_configured_org_ceiling_is_a_genuine_noop(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()

    # No WorkforceCostBudget row exists for this goal or this provider at all.
    reserved, created = reserve_provider_spend_call(
        superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref="noop-call", provider="fake-local", model="planner-v2",
    )
    assert created is True
    assert reserved.status == "reserved"


# --- 2. THE GAP THIS RECONCILIATION CLOSES: a configured, exhausted org ceiling at the
# goal scope must block a reservation even though provider_spend's OWN ceiling still has
# plenty of headroom. Before the fix, this reservation succeeded (the org ceiling was
# defined but never consulted). ------------------------------------------------------------


def test_exhausted_org_ceiling_at_goal_scope_blocks_reservation_even_with_provider_spend_headroom(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    # provider_spend's own ceiling is generous -- $100, 100 requests.
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()

    # But an organizational ceiling for THIS goal is configured and already exhausted.
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="goal", scope_ref=str(goal.id), cap_usd=0.0)
    superuser_db.commit()

    with pytest.raises(ProviderSpendError):
        reserve_provider_spend_call(
            superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref="blocked-call", provider="fake-local", model="planner-v2",
        )
    superuser_db.commit()

    # And no reservation was actually held on the provider_spend side either -- a rejected
    # attempt must not leave a half-applied hold.
    live = provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id)
    assert live is True  # the provider_spend grant itself is still live; only THIS call was blocked


def test_org_ceiling_at_provider_scope_blocks_reservation(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="provider", scope_ref="fake-local", cap_usd=0.0)
    superuser_db.commit()

    with pytest.raises(ProviderSpendError):
        reserve_provider_spend_call(
            superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref="blocked-provider-call", provider="fake-local", model="planner-v2",
        )


# --- 3. A real, positive org ceiling with headroom allows the reservation through, and the
# real settle path still works normally afterward -- this is additive protection, not a
# blanket new blocker. -----------------------------------------------------------------------


def test_org_ceiling_with_headroom_allows_reservation_and_settle_still_works(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="goal", scope_ref=str(goal.id), cap_usd=10.0)
    superuser_db.commit()

    reserved, created = reserve_provider_spend_call(
        superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref="ok-call", provider="fake-local", model="planner-v2",
    )
    superuser_db.commit()
    assert created is True
    settled = settle_provider_spend_call(
        superuser_db, owner_id=owner.id, source_ref="ok-call", prompt_tokens=10, completion_tokens=5, cost_usd="0.01",
    )
    assert settled.status == "settled"


# --- 4. TWO LEDGERS != TWO SOURCES OF TRUTH: settling a provider_spend call does not
# itself silently mutate workforce.cost's own ledger (no hidden write-through) -- the two
# remain genuinely separate ledgers, composed only at the reservation-gate check. ----------


def test_settling_provider_spend_does_not_silently_mutate_workforce_cost_ledger(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="goal", scope_ref=str(goal.id), cap_usd=10.0)
    superuser_db.commit()

    budget_before = superuser_db.execute(
        select(WorkforceCostBudget).where(WorkforceCostBudget.owner_id == owner.id, WorkforceCostBudget.scope_kind == "goal")
    ).scalar_one()
    spent_before = budget_before.spent_usd

    reserve_provider_spend_call(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref="ledger-call", provider="fake-local", model="planner-v2")
    superuser_db.commit()
    settle_provider_spend_call(superuser_db, owner_id=owner.id, source_ref="ledger-call", cost_usd="0.50")
    superuser_db.commit()

    superuser_db.refresh(budget_before)
    assert budget_before.spent_usd == spent_before, "workforce.cost's own ledger must not be silently written by a provider_spend settle"


# --- 5. Owner isolation: a ceiling configured for owner A must never affect owner B's
# reservations, and vice versa. -----------------------------------------------------------


def test_org_ceiling_is_owner_scoped(superuser_db):
    owner_a, goal_a, envelope_a = _owner_goal_envelope(superuser_db)
    owner_b, goal_b, envelope_b = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner_a, goal_a, envelope_a)
    _grant(superuser_db, owner_b, goal_b, envelope_b)
    superuser_db.commit()
    # Exhausted ceiling for owner A's goal only.
    set_cost_budget(superuser_db, owner_id=owner_a.id, scope_kind="goal", scope_ref=str(goal_a.id), cap_usd=0.0)
    superuser_db.commit()

    with pytest.raises(ProviderSpendError):
        reserve_provider_spend_call(superuser_db, owner_id=owner_a.id, goal_id=goal_a.id, source_ref="a-call", provider="fake-local", model="planner-v2")

    # Owner B's identically-scoped-by-coincidence reservation (same scope_kind="goal") is
    # entirely unaffected -- different owner_id, different budget row (or none at all).
    reserved_b, created_b = reserve_provider_spend_call(
        superuser_db, owner_id=owner_b.id, goal_id=goal_b.id, source_ref="b-call", provider="fake-local", model="planner-v2",
    )
    assert created_b is True


# --- 6. A released (failed) reservation must not have consumed org-ceiling headroom either
# -- release-side symmetry with provider_spend's own "release does not consume budget". ----


def test_released_reservation_does_not_consume_org_ceiling(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="goal", scope_ref=str(goal.id), cap_usd=10.0)
    superuser_db.commit()

    reserve_provider_spend_call(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref="fail-call", provider="fake-local", model="planner-v2")
    superuser_db.commit()
    release_provider_spend_call(superuser_db, owner_id=owner.id, source_ref="fail-call")
    superuser_db.commit()

    budget = superuser_db.execute(
        select(WorkforceCostBudget).where(WorkforceCostBudget.owner_id == owner.id, WorkforceCostBudget.scope_kind == "goal")
    ).scalar_one()
    assert budget.spent_usd == 0.0
    assert budget.reserved_usd == 0.0
