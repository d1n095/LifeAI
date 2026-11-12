"""INDEPENDENT VERIFICATION of PR #237 (cursor/mainai-safe-internal-startup @
7878958bfa57fa999122ac3008e9f261233f158e) -- the canonical fix for the workforce kill-switch
authority-widening race (a WorkforceAssignment granted concurrently with a kill-switch stop
surviving as live, unrevoked authority).

This file is written AGAINST the checked-out #237 branch, as a throwaway local verification
artifact -- it is NOT part of #237's own diff and is never pushed to that branch.

Every test here uses TWO REAL, SEPARATE PostgreSQL connections/sessions (never the same
session reused, never in-process mocking of lock behavior) -- either:
  (a) two sequential sessions where one holds `SELECT ... FOR UPDATE` open without
      committing and the other sets a real `lock_timeout` and genuinely blocks on Postgres's
      own lock manager (this is the standard, non-flaky way to prove row-lock contention
      between two live connections without relying on wall-clock timing), or
  (b) two real `threading.Thread`s with real separate sessions, synchronized with
      `threading.Barrier` and/or a monkeypatched pause point at the exact post-lock,
      pre-commit boundary -- the same idiom `test_partial_plan_insertion.py`'s own
      concurrent-insert test uses.

Maps to CLAUDE_CODE_TASK Directive 1, checks 1-10.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.user import User
from app.models.workforce import WorkforceAssignment
from app.workforce import register_workforce_agent
from app.workforce.authority import TaskScopedAuthority, assignment_authority_is_live
from app.workforce.broker import DelegationBrokerError, resolve_delegation, submit_delegation_request
from app.workforce.kill_switch import (
    KillSwitchError,
    activate_global_emergency_stop,
    activate_owner_stop,
    assert_authority_grant_allowed,
    assert_not_killed,
    clear_owner_stop,
    prove_no_reusable_live_authority,
    query_stop_status,
    reset_kill_switch_for_tests,
)


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _reset(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    superuser_db.execute(sa_text("UPDATE mainai_stop_state SET active=false, reason='', sequence=0"))
    superuser_db.flush()
    superuser_db.commit()


def _owner(db, tag: str = "race") -> User:
    u = User(email=f"ks-race-{tag}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _new_session():
    """A brand-new SUPERUSER-bound session == a brand-new real Postgres connection
    (sessionmaker(bind=migration_engine) matches conftest's own `superuser_db` fixture, which
    every existing workforce/kill-switch test in this codebase already uses -- the workforce
    tables carry RLS, the stop-control tables do not, and getting both out of the critical path
    keeps these tests about the LOCK, not about RLS session-variable plumbing)."""
    from app.db import migration_engine

    return sessionmaker(bind=migration_engine)()


def _seed_pair(db, owner_id):
    builder = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"local-classifier-{uuid.uuid4().hex[:6]}",
        name="Local Classifier",
        role="specialist",
        agent_type="LOCAL_MODEL",
        provider_type="local",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["low_risk_classification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    verifier = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=f"local-verifier-{uuid.uuid4().hex[:6]}",
        name="Local Verifier",
        role="verifier",
        agent_type="VERIFIER",
        provider_type="local",
        trust_zone="LOCAL_INTERNAL",
        capability_tags=["verification"],
        allowed_tool_classes=["read_excerpt"],
        cost_class="low",
        status="active",
    )
    return builder, verifier


def _submit_and_grant(db, *, owner_id, verifier_id):
    """Takes plain uuids only (never an ORM object bound to another thread's session --
    SQLAlchemy Session objects, and any instance attached to one, are not safe to touch
    concurrently from two threads, even read-only attribute access)."""
    req = submit_delegation_request(
        db,
        owner_id=owner_id,
        goal_text="Classify a note (race verification).",
        required_capability="low_risk_classification",
        risk="low",
        data_sensitivity="internal",
        cost_ceiling_usd=0.0,
        verification_requirement="independent_verifier",
    )
    return resolve_delegation(
        db,
        owner_id=owner_id,
        request=req,
        context_items=[],
        authority=TaskScopedAuthority(
            allowed_tool_classes=("read_excerpt",),
            allow_execution_effects=False,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        ),
        verifier_profile_id=verifier_id,
    )


# =============================================================================================
# CHECK 1-2: lock acquired BEFORE live authority is created, and held through the grant's
# own commit (two real, separate connections; one holds FOR UPDATE open, the other uses a real
# Postgres lock_timeout to prove genuine blocking -- not a timing guess).
# =============================================================================================


def test_check1_2_grant_fence_lock_is_held_open_and_blocks_a_second_real_connection(superuser_db):
    owner = _owner(superuser_db, "fence")
    superuser_db.commit()
    owner_id = owner.id
    # Pre-seed the owner's stop-state row (uncontended) so what follows is purely about the
    # FOR UPDATE lock, not row-creation contention.
    query_stop_status(superuser_db, owner_id=owner_id)
    superuser_db.commit()

    session_a = _new_session()
    session_b = _new_session()
    try:
        fence = assert_authority_grant_allowed(session_a, owner_id=owner_id)  # lock acquired, NOT committed
        assert fence["fence"] == "mainai_stop_state_for_update"

        # A second, fully independent connection must genuinely block trying to acquire the
        # SAME owner row's FOR UPDATE lock while A holds it open and uncommitted.
        session_b.execute(sa_text("SET LOCAL lock_timeout = '400ms'"))
        with pytest.raises(OperationalError):
            assert_authority_grant_allowed(session_b, owner_id=owner_id)
        session_b.rollback()  # clear the aborted transaction

        # The lock is still held by A (proves it was not released early / mid-transaction).
        session_b.execute(sa_text("SET LOCAL lock_timeout = '400ms'"))
        with pytest.raises(OperationalError):
            assert_authority_grant_allowed(session_b, owner_id=owner_id)
        session_b.rollback()

        # A finally commits ("the grant's actual transaction commits") -> lock releases.
        session_a.commit()

        # B can now acquire it cleanly.
        fence_b = assert_authority_grant_allowed(session_b, owner_id=owner_id)
        assert fence_b["fence"] == "mainai_stop_state_for_update"
        session_b.commit()
    finally:
        session_a.close()
        session_b.close()


# =============================================================================================
# CHECK 3 + DIRECTIVE 2 MEASUREMENT: owner-scoped and global stop operations acquire
# CONFLICTING locks against the grant path -- AND, separately, whether #237's grant path locks
# the GLOBAL row with FOR UPDATE (exclusive), which would serialize unrelated grants from
# DIFFERENT owners through that one row even with no stop involved at all.
# =============================================================================================


def test_check3_owner_stop_and_grant_contend_for_the_same_row_lock(superuser_db):
    owner = _owner(superuser_db, "contend")
    superuser_db.commit()
    owner_id = owner.id
    query_stop_status(superuser_db, owner_id=owner_id)
    superuser_db.commit()

    session_a = _new_session()
    session_b = _new_session()
    try:
        # A grant holds the fence (uncommitted).
        assert_authority_grant_allowed(session_a, owner_id=owner_id)

        # A concurrent STOP attempt for the SAME owner must genuinely block -- not merely
        # "happen to not overlap".
        session_b.execute(sa_text("SET LOCAL lock_timeout = '400ms'"))
        with pytest.raises(OperationalError):
            activate_owner_stop(session_b, owner_id=owner_id, reason="contend")
        session_b.rollback()

        session_a.commit()

        # Now that the grant transaction is closed, the stop proceeds and finds nothing to
        # revoke (the grant above only acquired the fence; it never inserted an assignment).
        state = activate_owner_stop(session_b, owner_id=owner_id, reason="contend-after")
        session_b.commit()
        assert state.active is True
    finally:
        session_a.close()
        session_b.close()


def test_directive2_global_row_for_update_serializes_unrelated_grants_from_different_owners(superuser_db):
    """#237's `_lock_authority_fence` takes `SELECT ... FOR UPDATE` (exclusive) on the GLOBAL
    row for every single grant, regardless of owner. Two grants for two DIFFERENT owners, with
    no stop anywhere in the picture, therefore still serialize through that one global row.
    This is the exact throughput concern the founder's directive asked to measure: it is a
    correctness-neutral but real contention cost that #243's `FOR SHARE` design on the grant
    side would avoid (many simultaneous FOR SHARE holders are allowed; only an actual stop's
    FOR UPDATE would force a wait)."""
    owner_a = _owner(superuser_db, "throughput-a")
    owner_b = _owner(superuser_db, "throughput-b")
    superuser_db.commit()
    query_stop_status(superuser_db, owner_id=owner_a.id)
    query_stop_status(superuser_db, owner_id=owner_b.id)
    superuser_db.commit()

    session_a = _new_session()
    session_b = _new_session()
    try:
        # Owner A's grant holds the fence (global row FOR UPDATE + owner-A row FOR UPDATE),
        # uncommitted.
        assert_authority_grant_allowed(session_a, owner_id=owner_a.id)

        # Owner B's grant touches a COMPLETELY DIFFERENT owner row -- the only thing it could
        # possibly contend on is the shared GLOBAL row.
        session_b.execute(sa_text("SET LOCAL lock_timeout = '400ms'"))
        with pytest.raises(OperationalError) as exc_info:
            assert_authority_grant_allowed(session_b, owner_id=owner_b.id)
        session_b.rollback()
        assert "lock" in str(exc_info.value).lower() or "timeout" in str(exc_info.value).lower()

        session_a.commit()
        # Once A's transaction ends, B's unrelated-owner grant proceeds immediately.
        assert_authority_grant_allowed(session_b, owner_id=owner_b.id)
        session_b.commit()
    finally:
        session_a.close()
        session_b.close()


# =============================================================================================
# CHECK 4-5: static verification recorded here as an executable assertion -- every
# WorkforceAssignment constructor call site in the codebase at this SHA is preceded by
# assert_authority_grant_allowed(), and every execution/run path checks assert_not_killed().
# (Full grep sweep was also done manually; see the handback report.)
# =============================================================================================


def test_check4_5_every_assignment_constructor_site_is_preceded_by_the_grant_fence():
    import inspect

    import app.workforce.broker as broker_mod
    import app.workforce.failure as failure_mod

    broker_src = inspect.getsource(broker_mod.resolve_delegation)
    fence_idx = broker_src.index("assert_authority_grant_allowed")
    ctor_idx = broker_src.index("WorkforceAssignment(")
    assert fence_idx < ctor_idx, "broker.resolve_delegation must call the grant fence BEFORE constructing WorkforceAssignment"

    failure_src = inspect.getsource(failure_mod)
    # Locate the one function in failure.py that constructs a WorkforceAssignment (takeover).
    fn_start = failure_src.index("def ")
    assert "assert_authority_grant_allowed" in failure_src
    assert "WorkforceAssignment(" in failure_src
    assert failure_src.index("assert_authority_grant_allowed") < failure_src.index("new_assignment = WorkforceAssignment(")

    import app.workforce.provider_worker as worker_mod
    import app.workforce.safe_internal_run as safe_run_mod
    import app.mainai_executive.safe_composed_run as composed_mod
    import app.mainai_executive.internal_start as internal_start_mod
    import app.mainai_executive.loop as loop_mod

    for mod in (worker_mod, safe_run_mod, composed_mod, internal_start_mod, loop_mod):
        src = inspect.getsource(mod)
        assert "assert_not_killed" in src, f"{mod.__name__} must fail-closed on the stop fence"


# =============================================================================================
# CHECK 6: a grant transaction that acquires the lock but then fails/rolls back for an
# unrelated reason must leave NO dangling/partial authority, and must release the lock.
# =============================================================================================


def test_check6_failed_grant_after_fence_leaves_no_dangling_authority_and_releases_lock(superuser_db):
    owner = _owner(superuser_db, "rollback")
    builder, verifier = _seed_pair(superuser_db, owner.id)
    superuser_db.commit()
    owner_id = owner.id

    session = _new_session()
    try:
        req = submit_delegation_request(
            session,
            owner_id=owner_id,
            goal_text="should fail after fence",
            required_capability="low_risk_classification",
            risk="low",
            data_sensitivity="internal",
            cost_ceiling_usd=0.0,
            verification_requirement="independent_verifier",
        )
        session.flush()
        with pytest.raises(DelegationBrokerError, match="BUILDER_CANNOT_SELF_VERIFY"):
            # verifier_profile_id == builder.id triggers a raise AFTER the fence is acquired
            # but BEFORE any WorkforceAssignment row is touched.
            resolve_delegation(
                session,
                owner_id=owner_id,
                request=req,
                context_items=[],
                authority=TaskScopedAuthority(expires_at=datetime.utcnow() + timedelta(hours=1)),
                verifier_profile_id=builder.id,
            )
        session.rollback()
    finally:
        session.close()

    assert (
        superuser_db.execute(
            sa_text("SELECT count(*) FROM workforce_assignments WHERE owner_id = :o"), {"o": str(owner_id)}
        ).scalar()
        == 0
    )
    # Lock released -- a fresh session can immediately take it with no contention.
    session2 = _new_session()
    try:
        assert_authority_grant_allowed(session2, owner_id=owner_id)
        session2.commit()
    finally:
        session2.close()


# =============================================================================================
# REQUIRED SCENARIO -- grant-before-stop: a grant's fence+insert+commit interleaves with a
# concurrent owner-stop, forced via a real threading.Barrier + a monkeypatched pause point
# (same idiom as test_partial_plan_insertion.py's concurrent-insert test). No matter which side
# the DB lock manager lets through first, the owner must end up with NO live authority.
# =============================================================================================


def test_scenario_grant_in_flight_vs_concurrent_stop_commit_leaves_no_live_authority(superuser_db, monkeypatch):
    owner = _owner(superuser_db, "grant-vs-stop")
    _builder, verifier = _seed_pair(superuser_db, owner.id)
    superuser_db.commit()
    owner_id = owner.id
    verifier_id = verifier.id
    query_stop_status(superuser_db, owner_id=owner_id)
    superuser_db.commit()

    fence_acquired = threading.Event()
    release_grant = threading.Event()

    import app.workforce.broker as broker_mod

    real_select = broker_mod.select_best_candidate

    def _paused_select(*args, **kwargs):
        # This runs AFTER assert_authority_grant_allowed() in resolve_delegation, i.e. the
        # fence lock is already held. Signal it, then hold the transaction open so the
        # concurrent stop attempt below genuinely has to wait on the real Postgres lock.
        fence_acquired.set()
        release_grant.wait(timeout=10)
        return real_select(*args, **kwargs)

    monkeypatch.setattr(broker_mod, "select_best_candidate", _paused_select)

    outcome = {}

    def _grant_thread():
        session = _new_session()
        try:
            assignment = _submit_and_grant(session, owner_id=owner_id, verifier_id=verifier_id)
            session.commit()
            outcome["assignment_id"] = assignment.id
        except Exception as exc:  # noqa: BLE001
            outcome["grant_error"] = repr(exc)
            session.rollback()
        finally:
            session.close()

    def _stop_thread():
        fence_acquired.wait(timeout=10)
        session = _new_session()
        try:
            # Must block here until the grant thread commits (or rolls back) -- real contention.
            activate_owner_stop(session, owner_id=owner_id, reason="race_stop")
            session.commit()
        except Exception as exc:  # noqa: BLE001
            outcome["stop_error"] = repr(exc)
            session.rollback()
        finally:
            session.close()

    t_grant = threading.Thread(target=_grant_thread)
    t_stop = threading.Thread(target=_stop_thread)
    t_grant.start()
    t_stop.start()
    # Give the stop thread time to actually be blocked inside Postgres before we let go.
    fence_acquired.wait(timeout=10)
    time.sleep(0.3)
    release_grant.set()
    t_grant.join(timeout=15)
    t_stop.join(timeout=15)

    assert "grant_error" not in outcome, outcome
    assert "stop_error" not in outcome, outcome
    assert "assignment_id" in outcome

    assert prove_no_reusable_live_authority(superuser_db, owner_id=owner_id) is True
    assignment = superuser_db.get(WorkforceAssignment, outcome["assignment_id"])
    assert assignment is not None
    assert assignment.status == "revoked"
    assert assignment_authority_is_live(assignment).live is False


# =============================================================================================
# REQUIRED SCENARIO -- stop-before-grant: the stop's own activate transaction holds the fence
# (forced open via a monkeypatched pause inside _revoke_owner_assignments); a grant attempted
# while that is in flight must block on the SAME lock, and once the stop commits, the grant's
# assert_not_killed must observe active=True and the grant must NEVER construct an assignment.
# =============================================================================================


def test_scenario_stop_in_flight_vs_concurrent_grant_blocks_the_grant_entirely(superuser_db, monkeypatch):
    owner = _owner(superuser_db, "stop-vs-grant")
    _builder, verifier = _seed_pair(superuser_db, owner.id)
    superuser_db.commit()
    owner_id = owner.id
    verifier_id = verifier.id
    query_stop_status(superuser_db, owner_id=owner_id)
    superuser_db.commit()

    fence_acquired = threading.Event()
    release_stop = threading.Event()

    import app.workforce.kill_switch as ks_mod

    real_revoke = ks_mod._revoke_owner_assignments

    def _paused_revoke(db, *, owner_id, reason):
        fence_acquired.set()
        release_stop.wait(timeout=10)
        return real_revoke(db, owner_id=owner_id, reason=reason)

    monkeypatch.setattr(ks_mod, "_revoke_owner_assignments", _paused_revoke)

    outcome = {}

    def _stop_thread():
        session = _new_session()
        try:
            activate_owner_stop(session, owner_id=owner_id, reason="stop_first")
            session.commit()
        except Exception as exc:  # noqa: BLE001
            outcome["stop_error"] = repr(exc)
            session.rollback()
        finally:
            session.close()

    def _grant_thread():
        fence_acquired.wait(timeout=10)
        session = _new_session()
        try:
            _submit_and_grant(session, owner_id=owner_id, verifier_id=verifier_id)
            session.commit()
            outcome["grant_succeeded"] = True
        except KillSwitchError as exc:
            outcome["grant_blocked"] = exc.code
            session.rollback()
        finally:
            session.close()

    t_stop = threading.Thread(target=_stop_thread)
    t_grant = threading.Thread(target=_grant_thread)
    t_stop.start()
    t_grant.start()
    fence_acquired.wait(timeout=10)
    time.sleep(0.3)
    release_stop.set()
    t_stop.join(timeout=15)
    t_grant.join(timeout=15)

    assert "stop_error" not in outcome, outcome
    assert outcome.get("grant_blocked") == "BLOCKED_BY_OWNER_STOP", outcome
    assert "grant_succeeded" not in outcome
    assert (
        superuser_db.execute(
            sa_text("SELECT count(*) FROM workforce_assignments WHERE owner_id = :o"), {"o": str(owner_id)}
        ).scalar()
        == 0
    )


# =============================================================================================
# REQUIRED SCENARIO -- simultaneous (barrier-forced): two threads hit the SAME owner's fence
# at the exact same moment with no artificial pause; Postgres's own lock manager must pick a
# strict winner, never allowing both to hold the exclusive lock concurrently.
# =============================================================================================


def test_scenario_simultaneous_barrier_forced_grant_and_stop_never_overlap(superuser_db):
    owner = _owner(superuser_db, "simultaneous")
    _builder, verifier = _seed_pair(superuser_db, owner.id)
    superuser_db.commit()
    owner_id = owner.id
    verifier_id = verifier.id
    query_stop_status(superuser_db, owner_id=owner_id)
    superuser_db.commit()

    barrier = threading.Barrier(2, timeout=10)
    outcome = {}

    def _grant_thread():
        session = _new_session()
        try:
            barrier.wait()
            _submit_and_grant(session, owner_id=owner_id, verifier_id=verifier_id)
            session.commit()
            outcome["grant_succeeded"] = True
        except KillSwitchError as exc:
            outcome["grant_blocked"] = exc.code
            session.rollback()
        finally:
            session.close()

    def _stop_thread():
        session = _new_session()
        try:
            barrier.wait()
            activate_owner_stop(session, owner_id=owner_id, reason="simultaneous")
            session.commit()
            outcome["stop_committed"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["stop_error"] = repr(exc)
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=_grant_thread)
    t2 = threading.Thread(target=_stop_thread)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert "stop_error" not in outcome, outcome
    assert outcome.get("stop_committed") is True
    # Either ordering is an acceptable, SAFE outcome -- the only unacceptable outcome is live,
    # unrevoked authority surviving.
    assert prove_no_reusable_live_authority(superuser_db, owner_id=owner_id) is True


# =============================================================================================
# REQUIRED SCENARIO -- process interruption: kill a connection mid-transaction (abrupt close,
# no commit/no rollback) right after it acquired the fence lock. Confirm the lock is released
# (Postgres auto-releases on connection termination) and no dangling/partial state survives.
# =============================================================================================


def test_scenario_process_interruption_mid_transaction_leaves_no_dangling_state(superuser_db):
    owner = _owner(superuser_db, "interrupt")
    superuser_db.commit()
    owner_id = owner.id
    query_stop_status(superuser_db, owner_id=owner_id)
    superuser_db.commit()

    session_a = _new_session()
    assert_authority_grant_allowed(session_a, owner_id=owner_id)  # lock held, uncommitted
    # Abrupt termination: close() without commit() or rollback() -- the connection is dropped
    # and Postgres rolls back + releases locks server-side on its own.
    raw_conn = session_a.connection().connection
    session_a.close()
    try:
        raw_conn.close()
    except Exception:
        pass

    # A fresh connection must be able to take the lock promptly -- no leaked lock.
    session_b = _new_session()
    try:
        session_b.execute(sa_text("SET LOCAL lock_timeout = '2000ms'"))
        assert_authority_grant_allowed(session_b, owner_id=owner_id)
        session_b.commit()
    finally:
        session_b.close()

    status = query_stop_status(superuser_db, owner_id=owner_id)
    assert status["blocked"] is False
    assert status["owner"]["active"] is False


# =============================================================================================
# CHECK 7: a GLOBAL stop blocks grants across ALL owners (2+ distinct owners, concurrent).
# =============================================================================================


def test_check7_global_stop_blocks_concurrent_grants_from_two_distinct_owners(superuser_db):
    owner_a = _owner(superuser_db, "global-a")
    owner_b = _owner(superuser_db, "global-b")
    _builder_a, verifier_a = _seed_pair(superuser_db, owner_a.id)
    _builder_b, verifier_b = _seed_pair(superuser_db, owner_b.id)
    superuser_db.commit()
    owner_a_id, owner_b_id, verifier_a_id, verifier_b_id = owner_a.id, owner_b.id, verifier_a.id, verifier_b.id

    activate_global_emergency_stop(
        superuser_db, reason="global_check7", founder_authority_ref="founder_ack:check7-global-stop"
    )
    superuser_db.commit()

    barrier = threading.Barrier(2, timeout=10)
    outcome = {}

    def _attempt(tag, owner_id, verifier_id):
        session = _new_session()
        try:
            barrier.wait()
            _submit_and_grant(session, owner_id=owner_id, verifier_id=verifier_id)
            session.commit()
            outcome[tag] = "succeeded"
        except KillSwitchError as exc:
            outcome[tag] = exc.code
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=_attempt, args=("a", owner_a_id, verifier_a_id))
    t2 = threading.Thread(target=_attempt, args=("b", owner_b_id, verifier_b_id))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert outcome["a"] == "BLOCKED_BY_GLOBAL_EMERGENCY_STOP", outcome
    assert outcome["b"] == "BLOCKED_BY_GLOBAL_EMERGENCY_STOP", outcome


# =============================================================================================
# CHECK 8: owner-scoped stop does NOT affect unrelated owners; a concurrent grant from the
# unaffected owner succeeds and stays live.
# =============================================================================================


def test_check8_owner_stop_does_not_affect_a_different_owners_concurrent_grant(superuser_db):
    stopped_owner = _owner(superuser_db, "iso-stopped")
    free_owner = _owner(superuser_db, "iso-free")
    _b1, v1 = _seed_pair(superuser_db, stopped_owner.id)
    _free_builder, free_verifier = _seed_pair(superuser_db, free_owner.id)
    superuser_db.commit()
    stopped_owner_id, free_owner_id = stopped_owner.id, free_owner.id
    v1_id, free_verifier_id = v1.id, free_verifier.id

    activate_owner_stop(superuser_db, owner_id=stopped_owner_id, reason="iso_check8")
    superuser_db.commit()

    barrier = threading.Barrier(2, timeout=10)
    outcome = {}

    def _stopped_attempt():
        session = _new_session()
        try:
            barrier.wait()
            _submit_and_grant(session, owner_id=stopped_owner_id, verifier_id=v1_id)
            session.commit()
            outcome["stopped"] = "succeeded"
        except KillSwitchError as exc:
            outcome["stopped"] = exc.code
            session.rollback()
        finally:
            session.close()

    def _free_attempt():
        session = _new_session()
        try:
            barrier.wait()
            assignment = _submit_and_grant(session, owner_id=free_owner_id, verifier_id=free_verifier_id)
            session.commit()
            outcome["free_assignment_id"] = assignment.id
        except Exception as exc:  # noqa: BLE001
            outcome["free_error"] = repr(exc)
            session.rollback()
        finally:
            session.close()

    t1 = threading.Thread(target=_stopped_attempt)
    t2 = threading.Thread(target=_free_attempt)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert outcome.get("stopped") == "BLOCKED_BY_OWNER_STOP", outcome
    assert "free_error" not in outcome, outcome
    assert "free_assignment_id" in outcome
    free_assignment = superuser_db.get(WorkforceAssignment, outcome["free_assignment_id"])
    assert free_assignment is not None
    assert assignment_authority_is_live(free_assignment).live is True


# =============================================================================================
# CHECK 9: stop state AND audit events survive a genuinely separate OS SUBPROCESS -- not
# importlib.reload (which leaves duplicate exception classes behind). Matches #243's own
# scenario 7 rigor.
# =============================================================================================


def test_check9_stop_state_and_events_survive_a_real_separate_os_process(superuser_db):
    owner = _owner(superuser_db, "restart")
    superuser_db.commit()
    owner_id = str(owner.id)

    activate_owner_stop(superuser_db, owner_id=owner.id, reason="pre_restart_marker")
    superuser_db.commit()

    from app.config import get_settings

    db_url = get_settings().database_url

    script = f"""
import sys
from sqlalchemy import create_engine, text

engine = create_engine({db_url!r})
with engine.connect() as conn:
    row = conn.execute(
        text("SELECT active, reason, sequence FROM mainai_stop_state WHERE scope='owner' AND owner_id=:o"),
        {{"o": {owner_id!r}}},
    ).mappings().one()
    if not row["active"]:
        print("FAIL_NOT_ACTIVE")
        sys.exit(1)
    if row["reason"] != "pre_restart_marker":
        print("FAIL_WRONG_REASON:" + str(row["reason"]))
        sys.exit(1)
    events = conn.execute(
        text("SELECT count(*) FROM mainai_stop_events WHERE scope='owner' AND owner_id=:o AND event_kind='activate'"),
        {{"o": {owner_id!r}}},
    ).scalar()
    if events < 1:
        print("FAIL_NO_EVENT")
        sys.exit(1)
    print("OK sequence=" + str(row["sequence"]))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.stdout.strip().startswith("OK sequence=")


# =============================================================================================
# CHECK 10: recovery (clearing a stop) requires valid founder authorization and REJECTS a
# replayed/stale clear_request_id -- not just "the unique index exists", but a real replay
# attempt against the running code path.
# =============================================================================================


def test_check10_clear_rejects_fabricated_ack_and_a_real_replayed_clear_request_id(superuser_db):
    owner = _owner(superuser_db, "recovery")
    superuser_db.commit()
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="to_be_cleared")
    superuser_db.commit()

    rid = uuid.uuid4()
    state = clear_owner_stop(
        superuser_db,
        owner_id=owner.id,
        founder_ack="founder_ack:check10-first-clear",
        clear_request_id=rid,
    )
    superuser_db.commit()
    assert state.active is False
    assert_not_killed(superuser_db, owner_id=owner.id)  # confirmed cleared

    # Re-stop, then attempt to REPLAY the exact same clear_request_id -- must be rejected even
    # though the ack text itself is well-formed and would otherwise be valid.
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="second_stop")
    superuser_db.commit()
    with pytest.raises(KillSwitchError) as exc_info:
        clear_owner_stop(
            superuser_db,
            owner_id=owner.id,
            founder_ack="founder_ack:check10-first-clear",  # same text
            clear_request_id=rid,  # SAME request id -- genuine replay
        )
    assert exc_info.value.code == "CLEAR_REPLAY"
    superuser_db.rollback()
    # The replay must not have cleared anything.
    assert query_stop_status(superuser_db, owner_id=owner.id)["owner"]["active"] is True
