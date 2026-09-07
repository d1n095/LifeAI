"""app.dev_director.budget_integration: real Program-local <-> real provider_spend/
workforce.cost composition. See docs/mainai_v2/MAINAI_V2_SPEND_AUTHORITY_RECONCILIATION.md.

Not wired into app.main/any router/the executive loop -- exercised only from these tests.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.dev_director import (
    BudgetIntegrationError,
    can_reserve_budget,
    new_program,
    release_unused_reservation,
    remaining_budget,
    reserve_budget,
    settle_job_cost,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.models.user import User
from app.provider_spend import authorize_provider_spend as grant_provider_spend
from app.workforce.cost import set_cost_budget


def _owner_goal_envelope(db):
    user = User(email=f"budget-int-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = create_goal(db, owner_id=user.id, title="budget integration probe", original_instruction="plan a local edit", created_by="test")
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
        max_cost_usd=Decimal("10.00"), max_requests=100, max_cost_per_request_usd=Decimal("1.00"),
        idempotency_key=f"spend-{uuid.uuid4()}", allowed_providers=["fake-local"], allowed_models=["planner-v2"],
    )
    kwargs.update(overrides)
    return grant_provider_spend(db, **kwargs)


def test_reserve_settle_round_trip_updates_program_local_envelope(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="test program", total_budget_ceiling_usd=10.0)

    event, budget = reserve_budget(
        superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id,
        source_ref="ri-1", provider="fake-local", model="planner-v2",
    )
    superuser_db.commit()
    original_reserved = float(event.reserved_cost_usd)
    assert budget.reserved_usd == original_reserved
    assert budget.consumed_usd == 0.0

    # Settling for LESS than the full reservation ($0.25 actual out of $1.00 held) is the
    # normal case, not an edge case -- the real provider_spend system fully clears the
    # reservation on settle regardless, and the Program-local envelope must mirror that
    # exactly, never leaving a phantom "still reserved" remainder.
    settled_event, budget = settle_job_cost(
        superuser_db, envelope=program.budget_envelope, owner_id=owner.id, source_ref="ri-1", provider="fake-local",
        reserved_amount_usd=original_reserved, actual_cost_usd="0.25",
    )
    assert budget.consumed_usd == 0.25
    assert budget.reserved_usd == 0.0


def test_release_unused_reservation_updates_program_local_envelope(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="test program", total_budget_ceiling_usd=10.0)
    event, budget = reserve_budget(
        superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id,
        source_ref="rel-1", provider="fake-local", model="planner-v2",
    )
    superuser_db.commit()
    reserved_amount = budget.reserved_usd
    assert reserved_amount > 0

    release_unused_reservation(
        superuser_db, envelope=program.budget_envelope, owner_id=owner.id, source_ref="rel-1", reserved_amount_usd=reserved_amount,
    )
    assert program.budget_envelope.reserved_usd == 0.0
    assert program.budget_envelope.consumed_usd == 0.0


def test_program_local_ceiling_tighter_than_real_systems_refuses_and_releases_real_reservation(superuser_db):
    """A composed-caller-configured Program budget can be TIGHTER than the real grant --
    reserve_budget() must refuse in that case and must not leave a dangling real reservation
    behind (it releases what it just took)."""
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope, max_cost_per_request_usd=Decimal("5.00"), max_cost_usd=Decimal("10.00"))
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="tight program", total_budget_ceiling_usd=0.01)

    with pytest.raises(BudgetIntegrationError):
        reserve_budget(
            superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id,
            source_ref="tight-1", provider="fake-local", model="planner-v2",
        )
    superuser_db.commit()

    # The real provider_spend reservation must have been released, not left dangling.
    from app.provider_spend.service import get_current_provider_spend_authorization

    auth = get_current_provider_spend_authorization(superuser_db, owner_id=owner.id, goal_id=goal.id)
    assert auth is not None
    assert float(auth.reserved_cost_usd) == 0.0


def test_can_reserve_budget_is_a_nonmutating_dry_run(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="dry run program", total_budget_ceiling_usd=10.0)
    result = can_reserve_budget(
        superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id, provider="fake-local", estimated_amount_usd=0.1,
    )
    assert result is True
    # A dry run must not have reserved anything against the real system.
    assert program.budget_envelope.reserved_usd == 0.0
    assert program.budget_envelope.consumed_usd == 0.0


def test_can_reserve_budget_false_when_workforce_cost_ceiling_exhausted(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="goal", scope_ref=str(goal.id), cap_usd=0.0)
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="blocked program", total_budget_ceiling_usd=10.0)
    result = can_reserve_budget(
        superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id, provider="fake-local", estimated_amount_usd=0.1,
    )
    assert result is False


def test_remaining_budget_is_the_minimum_of_all_three_ledgers(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope, max_cost_usd=Decimal("5.00"))
    superuser_db.commit()
    set_cost_budget(superuser_db, owner_id=owner.id, scope_kind="goal", scope_ref=str(goal.id), cap_usd=2.0)
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="min program", total_budget_ceiling_usd=100.0)
    remaining = remaining_budget(superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id, provider="fake-local")
    # The tightest of: Program's own 100.0, provider_spend's 5.00, workforce.cost's 2.0.
    assert remaining == 2.0


def test_remaining_budget_never_negative(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()

    program = new_program(owner_id=owner.id, repo_identity="lifeai", goal="tiny program", total_budget_ceiling_usd=0.0)
    remaining = remaining_budget(superuser_db, envelope=program.budget_envelope, owner_id=owner.id, goal_id=goal.id, provider="fake-local")
    assert remaining == 0.0
