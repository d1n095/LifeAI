"""Provider-spend authorization foundation (migration 0060 / Autonomy Activation B1).

Proves REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY, and that the call boundary is
reserve → invoke → settle (not a boolean that leaves ceilings unused).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.models.provider_spend import (
    ProviderSpendAuthorizationStatus,
    ProviderSpendUsageStatus,
)
from app.models.user import User
from app.provider_spend import (
    ProviderSpendError,
    authorize_provider_spend,
    get_current_provider_spend_authorization,
    provider_spend_is_live,
    record_provider_spend_usage,
    release_provider_spend_call,
    reserve_provider_spend_call,
    revoke_provider_spend,
    settle_provider_spend_call,
)


def _owner_goal_envelope(db, *, paths=None, capabilities=None):
    user = User(email=f"spend-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = create_goal(
        db,
        owner_id=user.id,
        title="provider spend probe",
        original_instruction="plan a local edit",
        created_by="test",
    )
    db.flush()
    proposal = propose_execution_scope(
        db, owner_id=user.id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=paths if paths is not None else ["README.md"],
        authorized_capabilities=capabilities if capabilities is not None else ["read_file", "patch_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    db.flush()
    return user, goal, envelope


def _grant(db, owner, goal, envelope, **overrides):
    kwargs = dict(
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=3,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    kwargs.update(overrides)
    return authorize_provider_spend(db, **kwargs)


def test_envelope_alone_never_authorizes_provider_spend(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    assert get_current_provider_spend_authorization(superuser_db, owner_id=owner.id, goal_id=goal.id) is None
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id) is False
    assert (
        provider_spend_is_live(
            superuser_db, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=envelope.id
        )
        is False
    )


def test_authorize_is_idempotent_and_rejects_key_reuse_with_different_authority_fields(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    first = _grant(
        superuser_db,
        owner,
        goal,
        envelope,
        idempotency_key="same-key",
        max_requests=2,
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()
    replay = _grant(
        superuser_db,
        owner,
        goal,
        envelope,
        idempotency_key="same-key",
        max_requests=2,
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    assert replay.id == first.id

    with pytest.raises(ProviderSpendError, match="idempotency key reused"):
        _grant(
            superuser_db,
            owner,
            goal,
            envelope,
            idempotency_key="same-key",
            max_requests=2,
            allowed_providers=["fake-local", "openai"],
            allowed_models=["planner-v2"],
        )


def test_reserve_before_call_and_settle_consumes_budget(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(superuser_db, owner, goal, envelope, max_requests=2)
    superuser_db.commit()

    reserved = reserve_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="call-1",
        provider="fake-local",
        model="planner-v2",
    )
    superuser_db.commit()
    assert reserved.status == ProviderSpendUsageStatus.reserved.value
    superuser_db.refresh(row)
    assert row.reserved_requests == 1
    assert row.spent_requests == 0
    # Boolean alone is not enough — reserved capacity still counts against live headroom.
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id)

    settled = settle_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        source_ref="call-1",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd="0.01",
    )
    superuser_db.commit()
    assert settled.status == ProviderSpendUsageStatus.settled.value
    superuser_db.refresh(row)
    assert row.spent_requests == 1
    assert row.reserved_requests == 0
    assert row.spent_cost_usd == Decimal("0.010000")

    # Idempotent settle / reserve replay must not double-charge.
    again = reserve_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="call-1",
        provider="fake-local",
        model="planner-v2",
    )
    settle_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        source_ref="call-1",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd="0.01",
    )
    superuser_db.commit()
    assert again.id == settled.id
    superuser_db.refresh(row)
    assert row.spent_requests == 1


def test_release_after_failure_does_not_consume_budget(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(superuser_db, owner, goal, envelope, max_requests=1)
    superuser_db.commit()
    reserve_provider_spend_call(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="fail-1",
        provider="fake-local",
        model="planner-v2",
    )
    superuser_db.commit()
    release_provider_spend_call(superuser_db, owner_id=owner.id, source_ref="fail-1")
    superuser_db.commit()
    superuser_db.refresh(row)
    assert row.spent_requests == 0
    assert row.reserved_requests == 0
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id)


def test_two_owners_may_reuse_the_same_source_ref(superuser_db):
    owner_a, goal_a, env_a = _owner_goal_envelope(superuser_db)
    owner_b, goal_b, env_b = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner_a, goal_a, env_a)
    _grant(superuser_db, owner_b, goal_b, env_b)
    superuser_db.commit()
    shared = "shared-source-ref"
    a = reserve_provider_spend_call(
        superuser_db,
        owner_id=owner_a.id,
        goal_id=goal_a.id,
        source_ref=shared,
        provider="fake-local",
        model="planner-v2",
    )
    b = reserve_provider_spend_call(
        superuser_db,
        owner_id=owner_b.id,
        goal_id=goal_b.id,
        source_ref=shared,
        provider="fake-local",
        model="planner-v2",
    )
    superuser_db.commit()
    assert a.id != b.id
    assert a.owner_id == owner_a.id
    assert b.owner_id == owner_b.id


def test_concurrent_first_grants_only_one_active(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    owner_id, goal_id, envelope_id = owner.id, goal.id, envelope.id
    bind = superuser_db.get_bind()
    results = []
    errors = []

    def _attempt(key):
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=bind)
        session = Session()
        try:
            row = authorize_provider_spend(
                session,
                owner_id=owner_id,
                goal_id=goal_id,
                execution_envelope_id=envelope_id,
                authorized_by="founder",
                max_cost_usd="1.00",
                max_requests=2,
                max_cost_per_request_usd="0.50",
                idempotency_key=key,
                allowed_providers=["fake-local"],
                allowed_models=["planner-v2"],
            )
            session.commit()
            results.append(row.id)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(str(exc))
        finally:
            session.close()

    t1 = threading.Thread(target=_attempt, args=("concurrent-a",))
    t2 = threading.Thread(target=_attempt, args=("concurrent-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # One wins; the other either loses the unique race or serializes and supersedes.
    # Structural invariant: never more than one active grant.
    active = superuser_db.execute(
        text(
            "SELECT count(*) FROM provider_spend_authorizations "
            "WHERE owner_id = :o AND goal_id = :g AND status = 'active'"
        ),
        {"o": str(owner_id), "g": str(goal_id)},
    ).scalar()
    assert active == 1
    assert len(results) >= 1


def test_cannot_bound_cost_without_per_request_or_priced_model(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(
        superuser_db,
        owner,
        goal,
        envelope,
        max_cost_per_request_usd=None,
        allowed_providers=["mystery"],
        allowed_models=["mystery-v1"],
    )
    superuser_db.commit()
    with pytest.raises(ProviderSpendError, match="cannot bound USD cost"):
        reserve_provider_spend_call(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref="unbounded",
            provider="mystery",
            model="mystery-v1",
        )


def test_revoke_and_expiry_fail_closed(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    revoke_provider_spend(superuser_db, owner_id=owner.id, authorization_id=row.id, reason="stop")
    superuser_db.commit()
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id) is False

    owner2, goal2, envelope2 = _owner_goal_envelope(superuser_db)
    expired = _grant(
        superuser_db,
        owner2,
        goal2,
        envelope2,
        expires_at=datetime.utcnow() - timedelta(seconds=5),
    )
    superuser_db.commit()
    assert get_current_provider_spend_authorization(superuser_db, owner_id=owner2.id, goal_id=goal2.id) is None
    superuser_db.refresh(expired)
    assert expired.status == ProviderSpendAuthorizationStatus.expired.value


def test_record_provider_spend_usage_is_reserve_then_settle(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(superuser_db, owner, goal, envelope, max_requests=2)
    superuser_db.commit()
    event = record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="legacy-1",
        provider="fake-local",
        model="planner-v2",
        prompt_tokens=3,
        cost_usd="0.02",
    )
    superuser_db.commit()
    assert event.status == ProviderSpendUsageStatus.settled.value
    superuser_db.refresh(row)
    assert row.spent_requests == 1


def test_usage_events_are_append_only_without_settle_guc(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    event = record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref=f"append-only-{uuid.uuid4()}",
        provider="fake-local",
        model="planner-v2",
        cost_usd="0.01",
    )
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        superuser_db.execute(
            text("UPDATE provider_spend_usage_events SET cost_usd = 99 WHERE id = :id"),
            {"id": str(event.id)},
        )
    superuser_db.rollback()
