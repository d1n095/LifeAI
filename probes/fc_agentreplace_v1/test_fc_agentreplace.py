"""Priority 11 — Failure Recovery and Dynamic Agent Replacement (#314 lineage 2308b697).

DB-backed via the target's own tests.conftest. Tests whether work can safely transfer from an
unavailable provider to another authorized, capable agent WITHOUT losing provenance, duplicating
effects, or bypassing delegation authorization. Distinct from the already-documented #314
selector/broker defects (SELECTOR-ASSIGN-NAMESPACE, DELEGATE-BROKER-BYPASS) — those are NOT
re-litigated here.

Established by reading the code on this SHA: there is NO assignment-level reassignment primitive.
The only transfer mechanism is `take_over_lease` (lease-level write authority). Recovery is
therefore assembled from: take over the stale lease + stand up a successor assignment + terminate
the source. The suite tests each safety property of that mechanism and records one acceptance
requirement for the missing atomic step.

  AR1 (PASS)  recovery is vendor-neutral: a non-Cursor agent takes over a stale lease held by a
              Cursor agent; the Cursor holder is fenced. (Recovery survives Cursor being down.)
  AR2 (PASS)  recovery preserves provenance: the lease_taken_over event records old->new agent and
              old->new generation (auditable transfer).
  AR3 (PASS)  recovery does not duplicate work (single instance): a recovered retry for the same
              canonical work is idempotent -- "old completion cannot mint a second assignment".
              (Cross-instance duplication is the separate P10 CC1/CC4 finding, not repeated here.)
  AR4 (PASS)  delegation authorization is re-checked for a replacement: readiness is
              AGENT_UNAVAILABLE for an inactive replacement, and AUTHORITY_CHANGED when the goal's
              authorized instruction changed -- a replacement cannot run on stale authority.
  AR5 (PASS)  recovery cannot widen write scope: takeover keeps the lease's original allowed_paths,
              and a re-acquire that tries to widen is rejected (ScopeExpansionError).
  AR6 (xfail) ACCEPTANCE REQUIREMENT: the recovery primitive does not terminate the source
              assignment, and running->superseded is not an allowed transition, so recovery alone
              leaves a live `running` orphan. A correct recovery must atomically terminate the
              recovered-away source.

Read-only examiner; no paid calls; loopback only. first_cycle_go=false.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from _guards import _no_egress  # noqa: F401
from _harness import (
    advance,
    assignment_status,
    create_assignment,
    create_assignment_with_authority,
    lease_event_details,
    make_goal,
    make_owner,
    readiness,
    register_coord_agent,
)


def _acquire(db, assignment, agent_id, *, branch, worktree, paths, ttl=60):
    from app.agent_coordination.service import acquire_lease
    return acquire_lease(db, assignment=assignment, agent_id=agent_id, branch=branch,
                         worktree_path=worktree, allowed_paths=list(paths), mode="read_write", ttl_seconds=ttl)


def _expire(db, lease_id):
    db.execute(sa_text("UPDATE agent_scope_leases SET expires_at=:p WHERE id=:i"),
               {"p": datetime.utcnow() - timedelta(minutes=5), "i": lease_id})
    db.commit()


# ---------------------------------------------------------------------------
# AR1 (PASS) -- vendor-neutral recovery: a non-Cursor agent takes over a Cursor holder's stale lease.
# ---------------------------------------------------------------------------
def test_AR1_recovery_is_vendor_neutral(superuser_db, make_verified_user):
    from app.agent_coordination.service import LeaseFencingError, renew_lease, take_over_lease

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    cursor = register_coord_agent(superuser_db, agent_key=f"cursor-worker-{uuid.uuid4().hex[:6]}")
    codex = register_coord_agent(superuser_db, agent_key=f"codex-worker-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=cursor.id)
    advance(superuser_db, assignment, "ready", "running")
    res = _acquire(superuser_db, assignment, cursor.id, branch="b", worktree="/wt", paths=["backend/app/x/**"])
    superuser_db.commit()
    _expire(superuser_db, res.lease.id)

    # the Cursor provider is unavailable; a Codex agent recovers the stale lease
    taken = take_over_lease(superuser_db, lease_id=res.lease.id, owner_id=owner, new_agent_id=codex.id,
                            expected_generation=1, ttl_seconds=60)
    assert taken.agent_id == codex.id and taken.lease_generation == 2
    superuser_db.commit()
    # the old Cursor holder is fenced out
    with pytest.raises(LeaseFencingError):
        renew_lease(superuser_db, lease_id=res.lease.id, owner_id=owner, agent_id=cursor.id,
                    lease_generation=1, ttl_seconds=60)
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# AR2 (PASS) -- recovery preserves provenance of the transfer.
# ---------------------------------------------------------------------------
def test_AR2_recovery_records_provenance(superuser_db, make_verified_user):
    from app.agent_coordination.service import take_over_lease

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    old = register_coord_agent(superuser_db, agent_key=f"old-{uuid.uuid4().hex[:6]}")
    new = register_coord_agent(superuser_db, agent_key=f"new-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=old.id)
    advance(superuser_db, assignment, "ready", "running")
    res = _acquire(superuser_db, assignment, old.id, branch="b", worktree="/wt", paths=["backend/app/x/**"])
    superuser_db.commit()
    _expire(superuser_db, res.lease.id)

    take_over_lease(superuser_db, lease_id=res.lease.id, owner_id=owner, new_agent_id=new.id,
                    expected_generation=1, ttl_seconds=60)
    superuser_db.commit()

    details = lease_event_details(superuser_db, assignment_id=assignment.id, event_type="lease_taken_over")
    assert details, "no lease_taken_over provenance event recorded"
    d = details[-1]
    assert d["old_agent_id"] == str(old.id) and d["new_agent_id"] == str(new.id)
    assert d["old_generation"] == 1 and d["new_generation"] == 2


# ---------------------------------------------------------------------------
# AR3 (PASS) -- recovery does not duplicate work (single instance, canonical idempotency).
# ---------------------------------------------------------------------------
def test_AR3_recovered_retry_is_idempotent(make_verified_user):
    from app.agent_coordination.durable import DurableCoordinationService

    owner = uuid.uuid4()
    svc = DurableCoordinationService()
    r1 = svc.assign(agent_identity="cursor", workstream="ws", owner_id=str(owner), instruction_id="I1")
    assert r1.accepted and r1.outcome == "applied"
    # a recovery/retry for the SAME canonical work (even by a different vendor) does not mint a
    # second obligation -- "old completion cannot mint a second assignment"
    r2 = svc.assign(agent_identity="codex", workstream="ws", owner_id=str(owner), instruction_id="I1")
    assert r2.outcome == "idempotent" and r2.assignment_id == r1.assignment_id
    assert len(svc._obligations) == 1


# ---------------------------------------------------------------------------
# AR4 (PASS) -- a replacement is re-checked against authorization (fail-closed).
# ---------------------------------------------------------------------------
def test_AR4_replacement_authorization_is_rechecked(superuser_db, make_verified_user):
    from app.agent_coordination.service import (
        OUTCOME_AGENT_UNAVAILABLE, OUTCOME_AUTHORITY_CHANGED, OUTCOME_LEASE_REQUIRED,
    )
    from app.models.agent_coordination import CoordinationAgentStatus

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    agent = register_coord_agent(superuser_db, agent_key=f"rep-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment_with_authority(superuser_db, owner_id=owner, goal=goal, agent_id=agent.id)
    superuser_db.commit()

    # baseline: a properly-authorized, available replacement passes the availability + authority +
    # dependency gates (its only remaining precondition is acquiring a write lease) -- i.e. nothing
    # fails closed yet
    assert readiness(superuser_db, assignment).outcome == OUTCOME_LEASE_REQUIRED

    # the replacement agent is unavailable -> fail-closed
    agent.status = CoordinationAgentStatus.unavailable
    superuser_db.flush()
    assert readiness(superuser_db, assignment).outcome == OUTCOME_AGENT_UNAVAILABLE

    # the agent returns, but the goal's authorized instruction changed -> delegation authority is
    # no longer the one this assignment was created under -> fail-closed (no silent re-delegation)
    agent.status = CoordinationAgentStatus.active
    superuser_db.flush()
    superuser_db.execute(sa_text("UPDATE mainai_goals SET original_instruction=:v WHERE id=:i"),
                         {"v": "a DIFFERENT instruction the assignment was never authorized under", "i": str(goal.id)})
    superuser_db.commit()
    superuser_db.refresh(assignment)
    assert readiness(superuser_db, assignment).outcome == OUTCOME_AUTHORITY_CHANGED


# ---------------------------------------------------------------------------
# AR5 (PASS) -- recovery cannot widen write scope.
# ---------------------------------------------------------------------------
def test_AR5_recovery_cannot_widen_scope(superuser_db, make_verified_user):
    from app.agent_coordination.service import ScopeExpansionError, take_over_lease

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    old = register_coord_agent(superuser_db, agent_key=f"old-{uuid.uuid4().hex[:6]}")
    new = register_coord_agent(superuser_db, agent_key=f"new-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=old.id,
                                   paths=("backend/app/narrow/**",))
    advance(superuser_db, assignment, "ready", "running")
    res = _acquire(superuser_db, assignment, old.id, branch="b", worktree="/wt",
                   paths=["backend/app/narrow/**"])
    superuser_db.commit()
    _expire(superuser_db, res.lease.id)

    take_over_lease(superuser_db, lease_id=res.lease.id, owner_id=owner, new_agent_id=new.id,
                    expected_generation=1, ttl_seconds=60)
    superuser_db.commit()
    # the taken-over lease still fences the ORIGINAL narrow scope
    lease_paths = superuser_db.execute(
        sa_text("SELECT allowed_paths FROM agent_scope_leases WHERE id=:i"), {"i": res.lease.id}).scalar_one()
    assert list(lease_paths) == ["backend/app/narrow/**"]
    # a recovery attempt to WIDEN the scope on the same assignment's lease is rejected
    with pytest.raises(ScopeExpansionError):
        _acquire(superuser_db, assignment, new.id, branch="b", worktree="/wt",
                 paths=["backend/app/narrow/**", "backend/app/wide/**"])
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# AR6 (xfail -- ACCEPTANCE REQUIREMENT) -- recovery does not reap the source assignment.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="ACCEPTANCE REQUIREMENT (P11): take_over_lease transfers "
                   "write authority but does NOT terminate the recovered-away source assignment, "
                   "and running->superseded is not an allowed transition, so recovery alone leaves "
                   "a live `running` orphan. A correct dynamic replacement must atomically "
                   "terminate (supersede/fail) the source when its lease is taken over.")
def test_AR6_recovery_leaves_source_orphaned(superuser_db, make_verified_user):
    from app.agent_coordination.service import take_over_lease

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    old = register_coord_agent(superuser_db, agent_key=f"old-{uuid.uuid4().hex[:6]}")
    new = register_coord_agent(superuser_db, agent_key=f"new-{uuid.uuid4().hex[:6]}")
    source = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=old.id)
    advance(superuser_db, source, "ready", "running")
    res = _acquire(superuser_db, source, old.id, branch="b", worktree="/wt", paths=["backend/app/x/**"])
    superuser_db.commit()
    _expire(superuser_db, res.lease.id)

    # recovery: the stale lease is taken over by the new agent
    take_over_lease(superuser_db, lease_id=res.lease.id, owner_id=owner, new_agent_id=new.id,
                    expected_generation=1, ttl_seconds=60)
    superuser_db.commit()
    superuser_db.expire_all()

    # REQUIRED: the recovered-away source is terminated. ACTUAL: it is still a live `running` orphan.
    assert assignment_status(superuser_db, source.id) in ("superseded", "failed", "cancelled"), \
        f"source assignment left orphaned in status '{assignment_status(superuser_db, source.id)}' after recovery"
