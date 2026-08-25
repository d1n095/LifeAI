"""Provider-spend authorization foundation (migration 0060 / Autonomy Activation B1).

Proves REPO-WRITE AUTHORITY != PROVIDER-SPEND AUTHORITY: an active execution envelope alone
never makes provider_spend_is_live() true. Only an explicit founder grant with ceilings does.
Final production_entry wire is deliberately NOT under test here — leave that edge until
Claude's authority surface unlocks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.models.provider_spend import (
    ProviderSpendAuthorization,
    ProviderSpendAuthorizationStatus,
    ProviderSpendUsageEvent,
)
from app.models.user import User
from app.provider_spend import (
    ProviderSpendError,
    authorize_provider_spend,
    get_current_provider_spend_authorization,
    provider_spend_is_live,
    record_provider_spend_usage,
    revoke_provider_spend,
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
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["fake"],
        allowed_models=["fake-plan-v1"],
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


def test_authorize_provider_spend_requires_current_envelope(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()

    with pytest.raises(ProviderSpendError, match="current active"):
        authorize_provider_spend(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            execution_envelope_id=uuid.uuid4(),
            authorized_by="founder",
            max_cost_usd="0.50",
            max_requests=1,
            idempotency_key="bad-env",
        )

    row = _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    assert row.status == ProviderSpendAuthorizationStatus.active.value
    assert provider_spend_is_live(
        superuser_db, owner_id=owner.id, goal_id=goal.id, execution_envelope_id=envelope.id
    )


def test_authorize_is_idempotent_and_rejects_key_reuse_with_different_fields(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    first = _grant(superuser_db, owner, goal, envelope, idempotency_key="same-key", max_requests=2)
    superuser_db.commit()
    replay = _grant(superuser_db, owner, goal, envelope, idempotency_key="same-key", max_requests=2)
    assert replay.id == first.id

    with pytest.raises(ProviderSpendError, match="idempotency"):
        _grant(superuser_db, owner, goal, envelope, idempotency_key="same-key", max_requests=9)


def test_new_grant_supersedes_prior_active_without_mutating_it(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    prior = _grant(superuser_db, owner, goal, envelope, idempotency_key="prior")
    superuser_db.commit()
    newer = _grant(superuser_db, owner, goal, envelope, idempotency_key="newer", max_cost_usd="2.00")
    superuser_db.commit()

    superuser_db.refresh(prior)
    assert prior.status == ProviderSpendAuthorizationStatus.superseded.value
    assert newer.supersedes_authorization_id == prior.id
    assert get_current_provider_spend_authorization(superuser_db, owner_id=owner.id, goal_id=goal.id).id == newer.id


def test_envelope_reauth_without_new_spend_grant_fails_closed(superuser_db):
    owner, goal, old_envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, old_envelope)
    superuser_db.commit()
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id)

    proposal = propose_execution_scope(
        superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}"
    )
    _, new_envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    superuser_db.commit()

    assert new_envelope.id != old_envelope.id
    assert get_current_provider_spend_authorization(superuser_db, owner_id=owner.id, goal_id=goal.id) is None
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id) is False


def test_revoke_and_expiry_fail_closed(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    revoke_provider_spend(superuser_db, owner_id=owner.id, authorization_id=row.id, reason="founder stop")
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


def test_usage_accounting_ceilings_and_idempotent_retry(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(
        superuser_db,
        owner,
        goal,
        envelope,
        max_requests=2,
        max_cost_usd=Decimal("0.10"),
        max_prompt_tokens=100,
    )
    superuser_db.commit()

    first = record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="call-1",
        provider="fake",
        model="fake-plan-v1",
        prompt_tokens=40,
        completion_tokens=10,
        cost_usd="0.04",
    )
    superuser_db.commit()
    assert first is not None
    superuser_db.refresh(row)
    assert row.spent_requests == 1
    assert row.spent_cost_usd == Decimal("0.040000")

    replay = record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="call-1",
        provider="fake",
        model="fake-plan-v1",
        prompt_tokens=40,
        completion_tokens=10,
        cost_usd="0.04",
    )
    superuser_db.commit()
    assert replay.id == first.id
    superuser_db.refresh(row)
    assert row.spent_requests == 1

    with pytest.raises(ProviderSpendError, match="allowlisted"):
        record_provider_spend_usage(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref="call-bad-provider",
            provider="openai",
            model="gpt-x",
            cost_usd="0.01",
        )

    record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="call-2",
        provider="fake",
        model="fake-plan-v1",
        prompt_tokens=40,
        cost_usd="0.04",
    )
    superuser_db.commit()
    superuser_db.refresh(row)
    # Exact fill of max_requests marks the grant exhausted — further NEW spend fails closed.
    assert row.spent_requests == 2
    assert row.status == ProviderSpendAuthorizationStatus.exhausted.value
    assert provider_spend_is_live(superuser_db, owner_id=owner.id, goal_id=goal.id) is False

    with pytest.raises(ProviderSpendError, match="no live provider spend"):
        record_provider_spend_usage(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref="call-3",
            provider="fake",
            model="fake-plan-v1",
            cost_usd="0.01",
        )
    superuser_db.rollback()

    # Retry of an already-recorded source_ref after exhaustion must not raise and must not
    # invent additional spend.
    again = record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="call-1",
        provider="fake",
        model="fake-plan-v1",
        cost_usd="0.04",
    )
    assert again.id == first.id
    superuser_db.refresh(row)
    assert row.spent_requests == 2


def test_usage_rejects_over_ceiling_before_exact_fill_marks_exhausted(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    row = _grant(
        superuser_db,
        owner,
        goal,
        envelope,
        max_requests=2,
        max_cost_usd=Decimal("0.05"),
    )
    superuser_db.commit()
    record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref="cost-1",
        provider="fake",
        model="fake-plan-v1",
        cost_usd="0.04",
    )
    superuser_db.commit()
    with pytest.raises(ProviderSpendError, match="cost ceiling"):
        record_provider_spend_usage(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref="cost-2",
            provider="fake",
            model="fake-plan-v1",
            cost_usd="0.04",
        )
    superuser_db.commit()
    superuser_db.refresh(row)
    assert row.status == ProviderSpendAuthorizationStatus.exhausted.value
    assert row.spent_requests == 1


def test_usage_events_are_append_only(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    _grant(superuser_db, owner, goal, envelope)
    superuser_db.commit()
    event = record_provider_spend_usage(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        source_ref=f"append-only-{uuid.uuid4()}",
        provider="fake",
        model="fake-plan-v1",
        cost_usd="0.01",
    )
    superuser_db.commit()

    with pytest.raises(DBAPIError):
        superuser_db.execute(
            text("UPDATE provider_spend_usage_events SET cost_usd = 99 WHERE id = :id"),
            {"id": str(event.id)},
        )
    superuser_db.rollback()


def test_column_specific_set_null_on_supersedes_survives_delete(superuser_db):
    """Plain composite SET NULL would also null NOT NULL owner_id — same defect class as 0057."""
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    superuser_db.commit()
    prior = _grant(superuser_db, owner, goal, envelope, idempotency_key="setnull-prior")
    superuser_db.commit()
    newer = _grant(superuser_db, owner, goal, envelope, idempotency_key="setnull-newer")
    superuser_db.commit()
    assert newer.supersedes_authorization_id == prior.id

    # Direct DELETE is revoked for mainai_app; tests run as migration owner / superuser so
    # this probes the FK behavior itself.
    superuser_db.execute(
        text("DELETE FROM provider_spend_authorizations WHERE id = :id"),
        {"id": str(prior.id)},
    )
    superuser_db.commit()
    superuser_db.refresh(newer)
    assert newer.supersedes_authorization_id is None
    assert newer.owner_id == owner.id
