"""Priority 10 — Multi-Agent Coordination Under Concurrent Execution (#314 lineage 2308b697).

DB-backed via the target's own tests.conftest. Two independent coordination actors (two Sessions
on the shared engine, and/or two DurableCoordinationService objects) operate on the SAME durable
state. Distinct from the single-service lifecycle / basic-restart suites.

Classification per the assignment ("distinguish a real production invariant from an unsupported
capability; if multi-instance coordination is not implemented, document the precise missing
boundary with executable evidence rather than calling it PASS"):

  xfail(strict) = REQUIRED multi-instance invariant that is NOT upheld (a missing boundary,
                  documented with a deterministic reproduction; flips to XPASS->failure if fixed).
  pass          = a real multi-instance invariant that genuinely holds.

  CC1 (item 2)      concurrent assignment of the same canonical work -> DEFECT: both commit
                    (check-then-insert, no unique index). xfail.
  CC2 (items 3,6)   concurrent terminal transition (complete vs cancel) on one running
                    assignment -> DEFECT: both commit, two contradictory terminal events
                    (no row-condition guard / version on transition). xfail.
  CC3 (item 4)      a stale whole-ledger snapshot overwrites newer persisted state (no
                    optimistic concurrency on persist_owner_ledger). xfail.
  CC4 (item 5)      provider-event dedup is per-process (_seen_event_ids/_by_canonical are
                    in-memory per instance), so two instances both act on the same event. xfail.
  CC5 (items 8,9)   lease fencing + takeover: a stale holder loses authority, double-takeover is
                    rejected -> real DB-fenced recovery invariant. PASS.
  CC6 (items 1,12)  two instances read the same committed state; a status view reflects committed
                    DB rows, never one process's stale memory. PASS.
  CC7 (item 11)     owner isolation across workers at the service-query level (NOTE: not RLS-
                    backed, unlike the security-kernel tables -- see verdict). PASS.

Items 7 (STOP during multi-agent run) and 10 (verification concurrent with reassignment) are
addressed in the verdict (STOP is enforced at the cycle/execution layer, see fc314; #10 follows
from CC2 + CC5). Read-only examiner; no paid calls; loopback only. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from _guards import _no_egress  # noqa: F401
from _harness import (
    advance,
    canonical_key,
    create_assignment,
    make_goal,
    make_owner,
    new_session,
    nonterminal_count_for_key,
    register_coord_agent,
    run_barriered,
    terminal_event_count,
)


# ---------------------------------------------------------------------------
# CC1 (item 2) -- concurrent assignment of the same canonical work from two instances.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="MISSING BOUNDARY (P10 item 2): create_work_assignment's "
                   "duplicate-work check is a check-then-insert with no unique index backing "
                   "canonical_work_key (migration 0046 indexes it NON-uniquely), so two instances "
                   "each pass is_duplicate_canonical_work and both commit a non-terminal assignment "
                   "for the same work. Fix: partial unique index on (owner_id, canonical_work_key) "
                   "WHERE status NOT IN (terminal) AND parallel_exploration_group_id IS NULL.")
def test_CC1_concurrent_same_work_double_assignment(superuser_db, make_verified_user):
    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    a1 = register_coord_agent(superuser_db, agent_key=f"agent-a-{uuid.uuid4().hex[:6]}")
    a2 = register_coord_agent(superuser_db, agent_key=f"agent-b-{uuid.uuid4().hex[:6]}")
    superuser_db.commit()
    key = canonical_key(goal_id=goal.id)

    s1, s2 = new_session(), new_session()
    try:
        # instance 1 creates (flush, NOT yet committed)
        create_assignment(s1, owner_id=owner, goal_id=goal.id, agent_id=a1.id)
        # instance 2's duplicate-check SELECT cannot see instance 1's uncommitted row -> it also
        # passes the check and inserts a second assignment for the same canonical work
        create_assignment(s2, owner_id=owner, goal_id=goal.id, agent_id=a2.id)
        s2.commit()
        s1.commit()
    finally:
        s1.close(); s2.close()

    count = nonterminal_count_for_key(superuser_db, owner_id=owner, canonical_work_key=key)
    # REQUIRED: at most one non-terminal assignment per canonical work. ACTUAL: 2.
    assert count <= 1, f"two concurrent instances both created an assignment for the same work (count={count})"


# ---------------------------------------------------------------------------
# CC2 (items 3, 6) -- two instances racing a terminal transition on one running assignment.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="MISSING BOUNDARY (P10 items 3/6): transition_status reads "
                   "status in Python and issues an unconditional ORM UPDATE with no status-guarded "
                   "WHERE and no version column, so two instances both transition one running "
                   "assignment (complete vs cancel) and both record a terminal status_changed "
                   "event. Fix: guard the UPDATE on the expected current status (optimistic "
                   "concurrency) so exactly one terminal transition wins.")
def test_CC2_concurrent_terminal_transitions_both_commit(superuser_db, make_verified_user):
    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    agent = register_coord_agent(superuser_db, agent_key=f"agent-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=agent.id)
    advance(superuser_db, assignment, "ready", "running")
    superuser_db.commit()
    aid = assignment.id

    def complete(s, barrier):
        from app.agent_coordination.service import transition_status
        from app.models.agent_coordination import AgentWorkAssignment
        row = s.get(AgentWorkAssignment, aid)
        barrier.wait()
        transition_status(s, assignment=row, new_status="completed")
        s.commit()
        return "completed"

    def cancel(s, barrier):
        from app.agent_coordination.service import transition_status
        from app.models.agent_coordination import AgentWorkAssignment
        row = s.get(AgentWorkAssignment, aid)
        barrier.wait()
        transition_status(s, assignment=row, new_status="cancelled")
        s.commit()
        return "cancelled"

    run_barriered(complete, cancel)

    superuser_db.expire_all()
    terminal_events = (terminal_event_count(superuser_db, assignment_id=aid, to_status="completed")
                       + terminal_event_count(superuser_db, assignment_id=aid, to_status="cancelled"))
    # REQUIRED: exactly one terminal transition commits. ACTUAL: both do (2 contradictory events).
    assert terminal_events <= 1, f"two instances both committed a terminal transition (events={terminal_events})"


# ---------------------------------------------------------------------------
# CC3 (item 4) -- a stale whole-ledger snapshot overwrites newer persisted state.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="MISSING BOUNDARY (P10 item 4): persist_owner_ledger writes "
                   "the full in-memory DurableCoordinationService as ONE JSON blob into "
                   "provenance['durable_coordination'] with no version/optimistic-concurrency "
                   "guard, so a second instance that started without instance A's obligation "
                   "overwrites it wholesale -> lost update. Fix: version the snapshot (or move "
                   "obligations to per-row storage) and reject a stale-base write.")
def test_CC3_stale_snapshot_overwrites_newer_state(superuser_db, make_verified_user):
    from app.agent_coordination.durable import (
        DurableCoordinationService, canonical_work_key, load_owner_ledger,
    )
    from app.agent_coordination.persist import persist_owner_ledger
    owner = make_owner(make_verified_user)
    superuser_db.commit()

    # instance A: learns obligation alpha and persists it
    sA = new_session()
    svcA = load_owner_ledger(sA, owner_id=owner)
    svcA.assign(agent_identity="claude", workstream="alpha", owner_id=str(owner), instruction_id="IA")
    persist_owner_ledger(sA, owner_id=owner, service=svcA)
    sA.commit(); sA.close()

    # instance B: a concurrently-running process that started empty (never saw alpha), learns
    # obligation beta, and persists -- overwriting the shared blob wholesale
    sB = new_session()
    svcB = DurableCoordinationService()
    svcB.assign(agent_identity="codex", workstream="beta", owner_id=str(owner), instruction_id="IB")
    persist_owner_ledger(sB, owner_id=owner, service=svcB)
    sB.commit(); sB.close()

    sC = new_session()
    try:
        reloaded = load_owner_ledger(sC, owner_id=owner)
    finally:
        sC.close()
    keyA = canonical_work_key(owner_id=str(owner), workstream="alpha", instruction_id="IA")
    keyB = canonical_work_key(owner_id=str(owner), workstream="beta", instruction_id="IB")
    present = set(reloaded._by_canonical.keys())
    # REQUIRED: both obligations survive two instances' commits. ACTUAL: alpha was overwritten.
    assert keyA in present and keyB in present, f"lost update: persisted ledger kept only {present}"


# ---------------------------------------------------------------------------
# CC4 (item 5) -- provider-event dedup is per-process, so two instances both act on one event.
# ---------------------------------------------------------------------------
@pytest.mark.xfail(strict=True, reason="MISSING BOUNDARY (P10 item 5): provider/event dedup lives "
                   "in per-process in-memory state (_seen_event_ids / _by_canonical), so two "
                   "instances (e.g. after simultaneous restart, both loaded the pre-event snapshot) "
                   "each accept the SAME provider event_id and both act on it. Fix: a shared, "
                   "transactional dedup key (unique index on the provider event id).")
def test_CC4_provider_event_dedup_is_per_process(make_verified_user):
    from app.agent_coordination.durable import DurableCoordinationService
    owner = uuid.uuid4()
    event_id = f"provider-evt-{uuid.uuid4().hex}"

    svc1 = DurableCoordinationService()
    svc2 = DurableCoordinationService()
    r1 = svc1.assign(agent_identity="claude", workstream="w", owner_id=str(owner),
                     instruction_id="I", event_id=event_id)
    r2 = svc2.assign(agent_identity="claude", workstream="w", owner_id=str(owner),
                     instruction_id="I", event_id=event_id)

    assert r1.accepted  # first instance acts on the event
    # REQUIRED: the second instance recognises the same event as already handled. ACTUAL: it does
    # not (separate in-memory dedup) and acts on it a second time.
    assert r2.outcome in ("replay_ignored", "idempotent"), f"second instance re-acted (outcome={r2.outcome})"


# ---------------------------------------------------------------------------
# CC5 (items 8, 9) -- lease fencing + takeover is DB-fenced (real recovery invariant). PASS.
# ---------------------------------------------------------------------------
def test_CC5_lease_takeover_fences_stale_holder(superuser_db, make_verified_user):
    from datetime import datetime, timedelta
    from app.agent_coordination.service import (
        LeaseFencingError, LeaseStillLiveError, acquire_lease, renew_lease, take_over_lease,
    )
    from app.models.agent_coordination import AgentScopeLease
    from sqlalchemy import text as sa_text

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    a_old = register_coord_agent(superuser_db, agent_key=f"old-{uuid.uuid4().hex[:6]}")
    a_new = register_coord_agent(superuser_db, agent_key=f"new-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=a_old.id)
    advance(superuser_db, assignment, "ready", "running")
    res = acquire_lease(superuser_db, assignment=assignment, agent_id=a_old.id, branch="b1",
                        worktree_path="/wt/1", allowed_paths=["backend/app/x/**"], mode="read_write",
                        ttl_seconds=60)
    assert res.outcome == "ACQUIRED"
    lease = res.lease
    superuser_db.commit()

    # a live lease cannot be taken over
    with pytest.raises(LeaseStillLiveError):
        take_over_lease(superuser_db, lease_id=lease.id, owner_id=owner, new_agent_id=a_new.id,
                        expected_generation=1, ttl_seconds=60)
    superuser_db.rollback()

    # the holder becomes unavailable: expire its lease (TTL elapsed)
    superuser_db.execute(sa_text("UPDATE agent_scope_leases SET expires_at=:p WHERE id=:i"),
                         {"p": datetime.utcnow() - timedelta(minutes=5), "i": lease.id})
    superuser_db.commit()

    # recovery: a new agent takes over the stale lease (generation bumps 1 -> 2)
    taken = take_over_lease(superuser_db, lease_id=lease.id, owner_id=owner, new_agent_id=a_new.id,
                            expected_generation=1, ttl_seconds=60)
    assert taken.lease_generation == 2 and taken.agent_id == a_new.id
    superuser_db.commit()

    # the old holder is fenced out: it can no longer renew with its old generation
    with pytest.raises(LeaseFencingError):
        renew_lease(superuser_db, lease_id=lease.id, owner_id=owner, agent_id=a_old.id,
                    lease_generation=1, ttl_seconds=60)
    superuser_db.rollback()

    # a second concurrent takeover presenting the now-stale expected_generation is rejected
    superuser_db.execute(sa_text("UPDATE agent_scope_leases SET expires_at=:p WHERE id=:i"),
                         {"p": datetime.utcnow() - timedelta(minutes=5), "i": lease.id})
    superuser_db.commit()
    with pytest.raises(LeaseFencingError):
        take_over_lease(superuser_db, lease_id=lease.id, owner_id=owner, new_agent_id=a_old.id,
                        expected_generation=1, ttl_seconds=60)
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# CC6 (items 1, 12) -- two instances read the same committed state; status view is from the DB.
# ---------------------------------------------------------------------------
def test_CC6_status_reflects_committed_state_not_stale_memory(superuser_db, make_verified_user):
    from app.agent_coordination.runtime_view import work_registry_snapshot
    from app.agent_coordination.service import transition_status
    from app.models.agent_coordination import AgentWorkAssignment

    owner = make_owner(make_verified_user)
    goal = make_goal(superuser_db, owner)
    agent = register_coord_agent(superuser_db, agent_key=f"agent-{uuid.uuid4().hex[:6]}")
    assignment = create_assignment(superuser_db, owner_id=owner, goal_id=goal.id, agent_id=agent.id)
    advance(superuser_db, assignment, "ready", "running")
    superuser_db.commit()
    aid = assignment.id

    # instance 2 (fresh session) reads the committed state
    s2 = new_session()
    try:
        views = {v.assignment_id: v for v in work_registry_snapshot(s2, owner_id=owner, goal_id=goal.id)}
        assert str(aid) in {str(k) for k in views} or aid in views
    finally:
        s2.close()

    # instance 1 commits a transition; a brand-new status read must reflect it (not stale memory)
    s1 = new_session()
    try:
        row = s1.get(AgentWorkAssignment, aid)
        transition_status(s1, assignment=row, new_status="completed")
        s1.commit()
    finally:
        s1.close()

    # a brand-new instance's per-assignment status view reflects the committed transition
    # (work_registry_snapshot intentionally lists only non-terminal work, so use the
    # per-assignment runtime view, which reads the committed row each call -- not stale memory)
    s3 = new_session()
    try:
        from app.agent_coordination.runtime_view import assignment_runtime_view
        row3 = s3.get(AgentWorkAssignment, aid)
        view = assignment_runtime_view(s3, row3)
        assert view.canonical_status == "completed"
    finally:
        s3.close()


# ---------------------------------------------------------------------------
# CC7 (item 11) -- owner isolation across workers at the service-query level. PASS (+ NOTE).
# ---------------------------------------------------------------------------
def test_CC7_owner_isolation_across_workers(superuser_db, make_verified_user):
    from app.agent_coordination.runtime_view import work_registry_snapshot

    owner_a = make_owner(make_verified_user)
    owner_b = make_owner(make_verified_user)
    goal_a = make_goal(superuser_db, owner_a)
    goal_b = make_goal(superuser_db, owner_b)
    agent_a = register_coord_agent(superuser_db, agent_key=f"a-{uuid.uuid4().hex[:6]}")
    agent_b = register_coord_agent(superuser_db, agent_key=f"b-{uuid.uuid4().hex[:6]}")
    asg_a = create_assignment(superuser_db, owner_id=owner_a, goal_id=goal_a.id, agent_id=agent_a.id)
    asg_b = create_assignment(superuser_db, owner_id=owner_b, goal_id=goal_b.id, agent_id=agent_b.id)
    superuser_db.commit()

    s = new_session()
    try:
        a_ids = {str(v.assignment_id) for v in work_registry_snapshot(s, owner_id=owner_a)}
        b_ids = {str(v.assignment_id) for v in work_registry_snapshot(s, owner_id=owner_b)}
    finally:
        s.close()
    assert str(asg_a.id) in a_ids and str(asg_b.id) not in a_ids
    assert str(asg_b.id) in b_ids and str(asg_a.id) not in b_ids
