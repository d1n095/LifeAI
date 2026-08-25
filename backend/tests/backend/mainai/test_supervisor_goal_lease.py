"""`supervisor_goal_leases` (migration 0059) claim/renew/release -- the durable mutual-
exclusion primitive `app/development_supervisor/production_entry.py`'s real worker trigger
depends on for crash/retry/concurrency safety. See that migration's own module docstring for
why this is a distinct lease from `mainai_jobs`' one-shot claim machinery.

Covers the founder decision's own explicit attack list (section 10): two workers racing the
same goal, stale-lease takeover after genuine expiry (never before), release makes the goal
immediately reclaimable (not "wait out the TTL"), and the fencing re-verification on renew/
release."""

import uuid

import pytest

from app.development_supervisor.lease import (
    SupervisorLeaseLostError,
    claim_supervisor_goal_lease,
    release_supervisor_goal_lease,
    renew_supervisor_goal_lease,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal


def _goal_and_envelope(db, owner_id):
    goal = create_goal(db, owner_id=owner_id, title="lease test goal", original_instruction="do work", created_by="test")
    db.flush()
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"lease-test-prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=[], authorized_risk="low",
        envelope_idempotency_key=f"lease-test-env-{uuid.uuid4()}",
    )
    return goal, envelope


def test_claim_succeeds_when_no_lease_exists_yet(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    claim = claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=60)
    assert claim is not None
    lease_id, generation = claim
    assert generation == 1


def test_a_second_worker_cannot_claim_a_still_valid_lease(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=300)
    second = claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-b", lease_seconds=300)
    assert second is None


def test_an_expired_lease_is_taken_over_in_place_not_duplicated(superuser_db, make_verified_user):
    from sqlalchemy import text

    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    first_id, first_generation = claim_supervisor_goal_lease(
        superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=300
    )
    superuser_db.execute(text("UPDATE supervisor_goal_leases SET expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(first_id)})
    superuser_db.commit()

    second = claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-b", lease_seconds=300)
    assert second is not None
    second_id, second_generation = second
    assert second_id == first_id  # same row, taken over in place
    assert second_generation == first_generation + 1

    from app.models.supervisor_lease import SupervisorGoalLease
    rows = superuser_db.query(SupervisorGoalLease).filter(SupervisorGoalLease.goal_id == goal.id).all()
    assert len(rows) == 1  # never duplicated
    assert rows[0].worker_id == "worker-b"


def test_release_makes_the_goal_immediately_reclaimable(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    lease_id, generation = claim_supervisor_goal_lease(
        superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=300
    )
    release_supervisor_goal_lease(superuser_db, lease_id=lease_id, worker_id="worker-a", lease_generation=generation)
    superuser_db.commit()

    second = claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-b", lease_seconds=300)
    assert second is not None  # no need to wait out the TTL


def test_renew_extends_the_lease_and_requires_the_exact_current_generation(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    lease_id, generation = claim_supervisor_goal_lease(
        superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=60
    )
    renew_supervisor_goal_lease(superuser_db, lease_id=lease_id, worker_id="worker-a", lease_generation=generation, lease_seconds=300)

    with pytest.raises(SupervisorLeaseLostError):
        renew_supervisor_goal_lease(superuser_db, lease_id=lease_id, worker_id="worker-a", lease_generation=generation + 1, lease_seconds=300)

    with pytest.raises(SupervisorLeaseLostError):
        renew_supervisor_goal_lease(superuser_db, lease_id=lease_id, worker_id="worker-b", lease_generation=generation, lease_seconds=300)


def test_release_requires_the_exact_current_generation_and_worker(superuser_db, make_verified_user):
    """A worker whose lease was already reclaimed by someone else (stale generation) cannot
    release the NEW claimant's lease out from under them -- release() must be a no-op for a
    mismatched (worker_id, generation), never a force-release."""
    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    lease_id, generation = claim_supervisor_goal_lease(
        superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=60
    )
    # worker-b releasing with a wrong generation/worker_id must not affect worker-a's real lease.
    release_supervisor_goal_lease(superuser_db, lease_id=lease_id, worker_id="worker-b", lease_generation=generation)
    superuser_db.commit()

    from app.models.supervisor_lease import SupervisorGoalLease
    row = superuser_db.query(SupervisorGoalLease).filter(SupervisorGoalLease.id == lease_id).one()
    assert row.status == "active"  # untouched


def test_two_workers_racing_the_same_goal_only_one_wins(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    goal, envelope = _goal_and_envelope(superuser_db, owner.id)

    claim_a = claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-a", lease_seconds=300)
    claim_b = claim_supervisor_goal_lease(superuser_db, owner_id=owner.id, goal_id=goal.id, envelope_id=envelope.id, worker_id="worker-b", lease_seconds=300)

    assert (claim_a is None) != (claim_b is None)  # exactly one wins
