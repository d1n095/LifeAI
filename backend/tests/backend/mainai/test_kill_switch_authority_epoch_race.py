"""AUTHORITY-WIDENING KILL-SWITCH RACE (P0) -- real two-connection/two-session reproductions
and the fix's regression coverage.

THE BUG (found by red-team review of PR #234's activation-prep changes, on top of PR #239's
owner-scoping fix): `activate_kill_switch()`'s "revoke all live assignments" SELECT was not
serialized in any way against a genuinely concurrent NEW assignment grant
(`app/workforce/broker.py`'s `resolve_delegation()`) on a separate DB connection. Production
window: the kill switch's own live-assignments SELECT returns empty, the concurrent grant
fully commits, then the kill switch's own commit lands -- the just-granted
`WorkforceAssignment` survives PERMANENTLY as live, unrevoked execution authority while the
kill switch itself reports active=True. `prove_no_reusable_live_authority()` (the system's
own safety-invariant oracle, unchanged by this fix -- see its docstring in kill_switch.py for
exactly what "live" means to the system itself) correctly returns False for that state.
Confirmed empirically pre-fix via git-stash negative control (see PR description) for the two
core interleavings this file's scenario 1 and 2 tests mirror.

THE FIX (`app/workforce/kill_switch.py`'s `assert_grant_allowed`/`activate_kill_switch`/
`activate_global_kill_switch`, migration 0069's `workforce_authority_epoch` table): every
scope (GLOBAL + one row per owner) gets a durable epoch row. The grant path
(`broker.resolve_delegation`) takes a `SELECT .. FOR SHARE` lock on the GLOBAL row then its
own owner's row, in the SAME transaction that creates the new assignment, before creating it,
and refuses if either scope is stopped. The stop path takes a conflicting
`SELECT .. FOR UPDATE` on the same row(s) as part of the SAME transaction that revokes live
assignments. Postgres's own row-lock manager -- not application-level timing -- then enforces
one strict ordering between any one grant and any one stop for a given scope.

Every scenario below asserts the SAME invariant post-race:
`prove_no_reusable_live_authority(db, owner_id=owner_id) is True` -- and, for the scenarios
where a grant should have been refused outright, that no assignment was ever created for it
either (not just "revoked after the fact").

Uses real separate SQLAlchemy sessions/threads/connections per the codebase's own established
concurrency-test idiom (see test_partial_plan_insertion.py's `threading.Barrier`-based
concurrent-insert test) -- never one session mutating state twice sequentially. Uses
superuser (RLS-bypassing) sessions throughout: the race being tested is pure DB row-locking,
orthogonal to RLS, and RLS setup would only add unrelated noise to these tests.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.user import User
from app.workforce import (
    KillSwitchError,
    activate_global_kill_switch,
    activate_kill_switch,
    assert_grant_allowed,
    clear_kill_switch_for_recovery,
    get_global_kill_switch,
    get_kill_switch,
    prove_no_reusable_live_authority,
    register_workforce_agent,
    reset_kill_switch_for_tests,
    resolve_delegation,
    submit_delegation_request,
)


def _new_session():
    """A fresh, independent SUPERUSER (RLS-bypassing) session/connection -- used to model a
    genuinely separate real request/worker, never the same session/transaction reused."""
    from app.db import migration_engine

    return sessionmaker(bind=migration_engine)()


def _owner(db):
    u = User(email=f"epoch-race-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _register_agents(db, owner_id):
    b = register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"b-{uuid.uuid4().hex[:8]}", name="Builder", role="builder",
        agent_type="CODING", capability_tags=["low_risk_classification"], status="active",
        trust_zone="LOCAL_INTERNAL", allowed_tool_classes=["read_excerpt"],
    )
    v = register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"v-{uuid.uuid4().hex[:8]}", name="Verifier", role="verifier",
        agent_type="VERIFIER", capability_tags=["verification"], status="active",
        trust_zone="LOCAL_INTERNAL",
    )
    return b, v


def _grant(session, *, owner_id, verifier_profile_id, label="race"):
    """The REAL grant path -- broker.resolve_delegation(), the exact function
    `assert_grant_allowed` was wired into. Raises KillSwitchError if refused."""
    req = submit_delegation_request(
        session, owner_id=owner_id, goal_text=f"grant {label}",
        required_capability="low_risk_classification", verification_requirement="independent_verifier",
    )
    return resolve_delegation(session, owner_id=owner_id, request=req, verifier_profile_id=verifier_profile_id)


@pytest.fixture(autouse=True)
def _reset():
    db = _new_session()
    try:
        reset_kill_switch_for_tests(db)
    finally:
        db.close()
    yield


# ---------------------------------------------------------------------------------------
# Scenario 1: grant starts first (invoked first), stop commits fully before the grant's own
# critical section (assert_grant_allowed) runs -- the grant must observe the committed stop
# and be refused BEFORE any assignment is created.
# ---------------------------------------------------------------------------------------
def test_scenario_1_grant_starts_first_stop_commits_before_grant_gate(superuser_db):
    setup = _new_session()
    owner = _owner(setup)
    _, v = _register_agents(setup, owner.id)
    owner_id, verifier_id = owner.id, v.id
    setup.commit()
    setup.close()

    grant_may_proceed = threading.Event()
    grant_started = threading.Event()
    result: dict = {}

    def _grant_thread():
        session = _new_session()
        try:
            grant_started.set()
            assert grant_may_proceed.wait(timeout=5), "stop did not signal in time"
            try:
                asg = _grant(session, owner_id=owner_id, verifier_profile_id=verifier_id, label="s1")
                session.commit()
                result["granted"] = True
                result["assignment_id"] = asg.id
            except KillSwitchError as exc:
                session.rollback()
                result["granted"] = False
                result["error"] = str(exc)
        finally:
            session.close()

    t = threading.Thread(target=_grant_thread)
    t.start()
    assert grant_started.wait(timeout=5)

    stop_session = _new_session()
    state = activate_kill_switch(stop_session, owner_id=owner_id, reason="scenario-1")
    stop_session.commit()
    stop_session.close()
    assert state.active is True

    grant_may_proceed.set()
    t.join(timeout=10)

    assert result.get("granted") is False, f"grant must be refused once the stop has committed, got: {result}"
    assert "KillSwitchError" in result["error"] or "kill switch active" in result["error"]

    verify = _new_session()
    try:
        assert prove_no_reusable_live_authority(verify, owner_id=owner_id) is True
    finally:
        verify.close()


# ---------------------------------------------------------------------------------------
# Scenario 2: stop starts first (invoked first) but the grant already holds the FOR SHARE
# lock (uncommitted) when the stop's activate_kill_switch runs -- the stop's FOR UPDATE
# request must genuinely BLOCK (proven via a liveness check, not assumed) until the grant
# commits, and must then find and revoke the just-committed assignment.
# ---------------------------------------------------------------------------------------
def test_scenario_2_stop_starts_first_grant_commits_before_stop_finishes(superuser_db):
    setup = _new_session()
    owner = _owner(setup)
    _, v = _register_agents(setup, owner.id)
    owner_id, verifier_id = owner.id, v.id
    setup.commit()
    setup.close()

    grant_locked = threading.Event()
    let_grant_finish = threading.Event()
    result: dict = {}

    def _grant_thread():
        session = _new_session()
        try:
            assert_grant_allowed(session, owner_id=owner_id)  # acquires+holds FOR SHARE
            grant_locked.set()
            assert let_grant_finish.wait(timeout=5), "main thread did not release grant in time"
            asg = _grant(session, owner_id=owner_id, verifier_profile_id=verifier_id, label="s2")
            session.commit()
            result["assignment_id"] = asg.id
        finally:
            session.close()

    t = threading.Thread(target=_grant_thread)
    t.start()
    assert grant_locked.wait(timeout=5)

    stop_result: dict = {}

    def _stop_thread():
        session = _new_session()
        try:
            stop_result["state"] = activate_kill_switch(session, owner_id=owner_id, reason="scenario-2")
            session.commit()
        finally:
            session.close()

    st = threading.Thread(target=_stop_thread)
    st.start()
    time.sleep(0.4)  # give the stop thread real time to reach and block on FOR UPDATE
    assert st.is_alive(), "stop must genuinely block on the grant's held FOR SHARE lock, not race ahead"

    let_grant_finish.set()
    t.join(timeout=10)
    st.join(timeout=10)

    assert "assignment_id" in result, "grant, which acquired the lock first, must have been allowed to commit"
    assert stop_result["state"].active is True
    assert str(result["assignment_id"]) in stop_result["state"].revoked_assignment_ids, (
        "the stop, once unblocked, must have found and revoked the assignment that committed while it waited"
    )

    verify = _new_session()
    try:
        assert prove_no_reusable_live_authority(verify, owner_id=owner_id) is True
    finally:
        verify.close()


# ---------------------------------------------------------------------------------------
# Scenario 3: as close to simultaneous as the test harness can force -- both threads race
# in from a shared barrier with no artificial ordering. Repeated across several iterations
# to flush out flakiness in either direction. Whichever wins, the invariant must hold.
# ---------------------------------------------------------------------------------------
def test_scenario_3_same_millisecond_barrier_race(superuser_db):
    for _ in range(6):
        setup = _new_session()
        owner = _owner(setup)
        _, v = _register_agents(setup, owner.id)
        owner_id, verifier_id = owner.id, v.id
        setup.commit()
        setup.close()

        barrier = threading.Barrier(2, timeout=5)
        outcome: dict = {}

        def _grant_thread():
            session = _new_session()
            try:
                barrier.wait()
                try:
                    asg = _grant(session, owner_id=owner_id, verifier_profile_id=verifier_id, label="s3")
                    session.commit()
                    outcome["grant"] = ("ok", asg.id)
                except KillSwitchError:
                    session.rollback()
                    outcome["grant"] = ("refused", None)
            finally:
                session.close()

        def _stop_thread():
            session = _new_session()
            try:
                barrier.wait()
                state = activate_kill_switch(session, owner_id=owner_id, reason="scenario-3")
                session.commit()
                outcome["stop"] = state
            finally:
                session.close()

        threads = [threading.Thread(target=_grant_thread), threading.Thread(target=_stop_thread)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        assert "grant" in outcome and "stop" in outcome
        assert outcome["stop"].active is True

        verify = _new_session()
        try:
            assert prove_no_reusable_live_authority(verify, owner_id=owner_id) is True, (
                f"invariant violated for outcome={outcome}"
            )
            # Consistency: if the grant succeeded, it must appear in what the stop revoked
            # OR the stop must have run before the grant even started (i.e. the grant, had
            # it re-checked, would have seen stopped=True -- but it reported success, so it
            # must be accounted for as revoked).
            if outcome["grant"][0] == "ok":
                assert str(outcome["grant"][1]) in outcome["stop"].revoked_assignment_ids
        finally:
            verify.close()


# ---------------------------------------------------------------------------------------
# Scenario 4: many concurrent grants (8) racing one stop. The GLOBAL row's FOR SHARE lock
# does not serialize grants against each other (many readers) -- only a real stop forces a
# barrier. Every grant must end up either cleanly refused or cleanly revoked; never live.
# ---------------------------------------------------------------------------------------
def test_scenario_4_many_concurrent_grants_racing_one_stop(superuser_db):
    setup = _new_session()
    owner = _owner(setup)
    _, v = _register_agents(setup, owner.id)
    owner_id, verifier_id = owner.id, v.id
    setup.commit()
    setup.close()

    n_grants = 8
    barrier = threading.Barrier(n_grants + 1, timeout=10)
    outcomes: list[tuple[str, uuid.UUID | None]] = []
    lock = threading.Lock()

    def _grant_thread(i):
        session = _new_session()
        try:
            barrier.wait()
            try:
                asg = _grant(session, owner_id=owner_id, verifier_profile_id=verifier_id, label=f"s4-{i}")
                session.commit()
                with lock:
                    outcomes.append(("ok", asg.id))
            except KillSwitchError:
                session.rollback()
                with lock:
                    outcomes.append(("refused", None))
        finally:
            session.close()

    stop_state: dict = {}

    def _stop_thread():
        session = _new_session()
        try:
            barrier.wait()
            stop_state["state"] = activate_kill_switch(session, owner_id=owner_id, reason="scenario-4")
            session.commit()
        finally:
            session.close()

    threads = [threading.Thread(target=_grant_thread, args=(i,)) for i in range(n_grants)]
    threads.append(threading.Thread(target=_stop_thread))
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)

    assert len(outcomes) == n_grants
    assert stop_state["state"].active is True
    revoked = set(stop_state["state"].revoked_assignment_ids)
    for kind, asg_id in outcomes:
        if kind == "ok":
            assert str(asg_id) in revoked, f"granted assignment {asg_id} was never revoked by the stop"

    verify = _new_session()
    try:
        assert prove_no_reusable_live_authority(verify, owner_id=owner_id) is True
    finally:
        verify.close()


# ---------------------------------------------------------------------------------------
# Scenario 5: owner-scoped stop must ONLY block/revoke that owner's grants. A concurrent
# grant for a completely different owner, racing at the same time, must be unaffected.
# ---------------------------------------------------------------------------------------
def test_scenario_5_owner_scoped_stop_only_affects_that_owner(superuser_db):
    setup = _new_session()
    owner_a = _owner(setup)
    owner_b = _owner(setup)
    _, v_a = _register_agents(setup, owner_a.id)
    _, v_b = _register_agents(setup, owner_b.id)
    owner_a_id, owner_b_id, v_b_id = owner_a.id, owner_b.id, v_b.id
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2, timeout=5)
    result_b: dict = {}

    def _grant_b_thread():
        session = _new_session()
        try:
            barrier.wait()
            asg = _grant(session, owner_id=owner_b_id, verifier_profile_id=v_b_id, label="s5-b")
            session.commit()
            result_b["assignment_id"] = asg.id
        finally:
            session.close()

    def _stop_a_thread():
        session = _new_session()
        try:
            barrier.wait()
            activate_kill_switch(session, owner_id=owner_a_id, reason="scenario-5-owner-a-only")
            session.commit()
        finally:
            session.close()

    threads = [threading.Thread(target=_grant_b_thread), threading.Thread(target=_stop_a_thread)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    assert "assignment_id" in result_b, "owner B's grant must succeed -- owner A's stop must not touch owner B"

    verify = _new_session()
    try:
        assert get_kill_switch(verify, owner_a_id).active is True
        assert get_kill_switch(verify, owner_b_id).active is False
        assert get_global_kill_switch(verify).active is False
        assert prove_no_reusable_live_authority(verify, owner_id=owner_a_id) is True
        # Owner B's assignment is legitimately live (never stopped) -- prove the SYSTEM
        # still reports it as live authority (i.e. we did not accidentally revoke it too).
        from app.models.workforce import WorkforceAssignment

        row = verify.get(WorkforceAssignment, result_b["assignment_id"])
        assert row.status == "assigned" and row.revoked_at is None
    finally:
        verify.close()


# ---------------------------------------------------------------------------------------
# Scenario 6: a TRUE global stop must block/revoke grants for every owner, not just one.
# ---------------------------------------------------------------------------------------
def test_scenario_6_global_stop_affects_all_owners(superuser_db):
    setup = _new_session()
    owner_a = _owner(setup)
    owner_b = _owner(setup)
    _, v_a = _register_agents(setup, owner_a.id)
    _, v_b = _register_agents(setup, owner_b.id)
    owner_a_id, owner_b_id, v_a_id, v_b_id = owner_a.id, owner_b.id, v_a.id, v_b.id
    setup.commit()
    setup.close()

    barrier = threading.Barrier(3, timeout=10)
    outcomes: dict = {}
    lock = threading.Lock()

    def _grant_thread(owner_id, verifier_id, key):
        session = _new_session()
        try:
            barrier.wait()
            try:
                asg = _grant(session, owner_id=owner_id, verifier_profile_id=verifier_id, label=f"s6-{key}")
                session.commit()
                with lock:
                    outcomes[key] = ("ok", asg.id)
            except KillSwitchError:
                session.rollback()
                with lock:
                    outcomes[key] = ("refused", None)
        finally:
            session.close()

    def _global_stop_thread():
        session = _new_session()
        try:
            barrier.wait()
            state = activate_global_kill_switch(session, reason="scenario-6-global")
            session.commit()
            with lock:
                outcomes["global_state"] = state
        finally:
            session.close()

    threads = [
        threading.Thread(target=_grant_thread, args=(owner_a_id, v_a_id, "a")),
        threading.Thread(target=_grant_thread, args=(owner_b_id, v_b_id, "b")),
        threading.Thread(target=_global_stop_thread),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)

    assert outcomes["global_state"].active is True
    revoked = set(outcomes["global_state"].revoked_assignment_ids)
    for key in ("a", "b"):
        kind, asg_id = outcomes[key]
        if kind == "ok":
            assert str(asg_id) in revoked, f"owner {key}'s granted assignment was never revoked by the global stop"

    verify = _new_session()
    try:
        assert get_global_kill_switch(verify).active is True
        assert prove_no_reusable_live_authority(verify, owner_id=owner_a_id) is True
        assert prove_no_reusable_live_authority(verify, owner_id=owner_b_id) is True
    finally:
        verify.close()

    # Clean up the global stop so it cannot leak into other test modules in this process.
    cleanup = _new_session()
    try:
        clear_kill_switch_for_recovery(cleanup, founder_ack="scenario-6-cleanup", owner_id=None)
        cleanup.commit()
    finally:
        cleanup.close()


# ---------------------------------------------------------------------------------------
# Scenario 7: "process restart" between the stop committing and a grant attempt. The grant
# is attempted from a genuinely SEPARATE OS PROCESS (a fresh Python interpreter that has
# never imported anything this test process has, sharing no module globals, no cached
# objects, no connection pool -- the strongest available approximation of "one
# uvicorn/gunicorn worker restarted") started AFTER the stop's commit. The stop must still
# be enforced, because durable state lives only in Postgres now -- there is no
# process-local state left anywhere for a restart to lose. (importlib.reload was
# deliberately avoided here: it would leave a second, distinct KillSwitchError class object
# in this test process, silently breaking `except KillSwitchError`/`pytest.raises` in every
# OTHER test module that already imported the original class and runs afterward in the same
# pytest session -- a real subprocess has no such cross-test pollution risk.)
# ---------------------------------------------------------------------------------------
def test_scenario_7_restart_between_operations_no_process_local_state(superuser_db):
    setup = _new_session()
    owner = _owner(setup)
    _, v = _register_agents(setup, owner.id)
    owner_id, verifier_id = owner.id, v.id
    setup.commit()
    setup.close()

    stop_session = _new_session()
    state = activate_kill_switch(stop_session, owner_id=owner_id, reason="scenario-7-pre-restart")
    stop_session.commit()
    stop_session.close()
    assert state.active is True
    del stop_session, state  # nothing left in THIS process referencing the stop either

    import os

    backend_root = str(Path(__file__).resolve().parents[3])  # backend/ -- so `import app...` resolves
    helper = Path(__file__).parent / "_kill_switch_restart_subprocess_helper.py"
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = backend_root + os.pathsep + child_env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(helper), str(owner_id), str(verifier_id)],
        cwd=backend_root,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"subprocess crashed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    assert lines and lines[0] == "REFUSED", (
        f"a brand new process (zero shared Python state) must still see the durably "
        f"committed stop and refuse the grant, got stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert lines[-1] == "PROVEN:True", f"post-restart invariant check failed: {proc.stdout!r}"
