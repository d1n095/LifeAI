"""F4 -- the Supervisor goal lease has a bounded lifecycle and a durable history.

Measured on 2026-11-27 in the isolated installation: `supervisor_goal_leases` gained one
`released` row per Supervisor tick (46+ for a single goal in a short run). The partial unique
index only constrains ACTIVE rows, so nothing stopped that growth, and nothing cleaned it up.

The fix keeps both properties that matter:

  * AT MOST ONE ROW PER GOAL in the live table -- `claim_supervisor_goal_lease()` recycles the
    goal's own released row (generation bumped, `released_at` cleared) instead of inserting a
    new one, and the expired-active takeover keeps mutating in place exactly as before;
  * NO LOST HISTORY -- `supervisor_goal_lease_events` is append-only and records every
    claim/takeover/release with its worker, generation and timestamps, written in the same
    transaction as the lease change.

Renewals are deliberately not events (no ownership change, and it would reintroduce the
per-tick growth). The tests below also prove the DB-level append-only enforcement and the
authorized owner-erasure path.
"""

import threading
import uuid

import pytest
from sqlalchemy import text as sa_text

from app.development_supervisor.lease import (
    claim_supervisor_goal_lease,
    release_supervisor_goal_lease,
    release_supervisor_goal_leases_from_previous_process,
)
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.founder import FOUNDER_USER_ID
from app.mainai_execution.planner import create_goal
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id=FOUNDER_USER_ID) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def goal_with_envelope(db_session, client):
    # `client` (not unused): entering the TestClient runs the app's startup, which bootstraps
    # the fixed founder user row this goal is owned by. Without it the FK to users.id fails.
    del client
    _set_rls_user(db_session)
    goal = create_goal(
        db_session,
        owner_id=FOUNDER_USER_ID,
        title="lease lifecycle",
        original_instruction="edit a file",
        created_by="test",
    )
    proposal = propose_execution_scope(
        db_session,
        owner_id=FOUNDER_USER_ID,
        goal_id=goal.id,
        idempotency_key=f"lease-lifecycle-proposal-{uuid.uuid4()}",
        proposed_paths=["README.md"],
        proposed_capabilities=["read_file"],
        proposed_risk="low",
    )
    _, envelope = authorize_execution_scope(
        db_session,
        owner_id=FOUNDER_USER_ID,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"lease-lifecycle-envelope-{uuid.uuid4()}",
    )
    db_session.commit()
    return goal, envelope


def _claim(db, goal, envelope, worker="worker-a", seconds=3600):
    _set_rls_user(db)
    return claim_supervisor_goal_lease(
        db,
        owner_id=goal.owner_id,
        goal_id=goal.id,
        envelope_id=envelope.id,
        worker_id=worker,
        lease_seconds=seconds,
    )


def _release(db, claim, worker="worker-a"):
    _set_rls_user(db)
    return release_supervisor_goal_lease(
        db, lease_id=claim[0], worker_id=worker, lease_generation=claim[1]
    )


def _lease_rows(db, goal):
    _set_rls_user(db)
    return db.execute(
        sa_text("SELECT id, status, lease_generation, worker_id FROM supervisor_goal_leases WHERE goal_id = :g ORDER BY acquired_at"),
        {"g": str(goal.id)},
    ).all()


def _events(db, goal):
    _set_rls_user(db)
    return db.execute(
        sa_text(
            "SELECT event_type, worker_id, lease_generation, acquired_at, released_at "
            "FROM supervisor_goal_lease_events WHERE goal_id = :g ORDER BY created_at, event_type"
        ),
        {"g": str(goal.id)},
    ).all()


def test_repeated_claims_recycle_one_row_and_keep_the_full_history(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    generations = []
    for index in range(5):
        claim = _claim(db_session, goal, envelope, worker=f"worker-{index}")
        assert claim is not None
        generations.append(claim[1])
        assert _release(db_session, claim, worker=f"worker-{index}") is True
        db_session.commit()

    rows = _lease_rows(db_session, goal)
    assert len(rows) == 1, "the live table must hold exactly one row per goal"
    assert rows[0].status == "released"
    assert generations == [1, 2, 3, 4, 5], "the recycled row is fenced forward, never reset"

    events = _events(db_session, goal)
    assert [event.event_type for event in events].count("claimed") == 5
    assert [event.event_type for event in events].count("released") == 5
    assert [event.worker_id for event in events if event.event_type == "claimed"] == [
        f"worker-{index}" for index in range(5)
    ]
    assert [event.lease_generation for event in events if event.event_type == "claimed"] == [1, 2, 3, 4, 5]
    for event in events:
        if event.event_type == "claimed":
            assert event.acquired_at is not None and event.released_at is None
        else:
            assert event.released_at is not None and event.acquired_at is None


def test_an_expired_active_lease_is_taken_over_in_place_and_recorded_as_taken_over(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    first = _claim(db_session, goal, envelope, worker="worker-a", seconds=1)
    db_session.commit()
    db_session.execute(
        sa_text("UPDATE supervisor_goal_leases SET expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": str(first[0])},
    )
    db_session.commit()

    second = _claim(db_session, goal, envelope, worker="worker-b", seconds=3600)
    db_session.commit()
    assert second is not None
    assert second[0] == first[0], "a takeover mutates the existing row, it never adds one"
    assert second[1] == first[1] + 1

    events = _events(db_session, goal)
    assert [event.event_type for event in events] == ["claimed", "taken_over"]
    assert events[-1].worker_id == "worker-b"
    assert len(_lease_rows(db_session, goal)) == 1


def test_a_live_lease_still_blocks_a_second_claim(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    first = _claim(db_session, goal, envelope, worker="worker-a")
    db_session.commit()
    assert _claim(db_session, goal, envelope, worker="worker-b") is None
    assert len(_lease_rows(db_session, goal)) == 1
    assert [event.event_type for event in _events(db_session, goal)] == ["claimed"]
    assert first is not None


def test_release_by_a_non_holder_writes_no_history(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    claim = _claim(db_session, goal, envelope, worker="worker-a")
    db_session.commit()
    assert _release(db_session, claim, worker="someone-else") is False
    db_session.commit()
    assert [event.event_type for event in _events(db_session, goal)] == ["claimed"]
    assert _lease_rows(db_session, goal)[0].status == "active"


def test_two_concurrent_claimers_cannot_create_a_second_active_row(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    db_session.commit()
    results: list[tuple | None] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _attempt(worker: str) -> None:
        from app.db import SessionLocal

        session = SessionLocal()
        try:
            _set_rls_user(session)
            barrier.wait(timeout=10)
            results.append(
                claim_supervisor_goal_lease(
                    session,
                    owner_id=goal.owner_id,
                    goal_id=goal.id,
                    envelope_id=envelope.id,
                    worker_id=worker,
                    lease_seconds=3600,
                )
            )
            session.commit()
        except BaseException as exc:  # noqa: BLE001 - surfaced below, never swallowed
            errors.append(exc)
            session.rollback()
        finally:
            session.close()

    threads = [threading.Thread(target=_attempt, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(results) == 2
    assert sum(1 for result in results if result is not None) == 1
    rows = _lease_rows(db_session, goal)
    assert len(rows) == 1 and rows[0].status == "active"
    assert [event.event_type for event in _events(db_session, goal)] == ["claimed"]


def test_the_event_history_is_append_only_at_the_database_layer(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    claim = _claim(db_session, goal, envelope)
    db_session.commit()

    # 1. The runtime role has no UPDATE/DELETE privilege on the history at all.
    for statement in (
        "UPDATE supervisor_goal_lease_events SET worker_id = 'tampered' WHERE goal_id = :g",
        "DELETE FROM supervisor_goal_lease_events WHERE goal_id = :g",
    ):
        with pytest.raises(Exception) as denied:
            db_session.execute(sa_text(statement), {"g": str(goal.id)})
        assert "permission denied" in str(denied.value).lower()
        db_session.rollback()

    # 2. Even the migrating/owner connection is stopped by the append-only trigger -- the
    #    invariant is enforced by the database, not only by the grant.
    from app.db import migration_engine

    with migration_engine.connect() as owner_conn:
        # FORCE ROW LEVEL SECURITY applies to the owner too, so the session context must be
        # present for the row to be visible at all -- otherwise this would prove only that an
        # invisible row cannot be updated.
        owner_conn.execute(
            sa_text("SELECT set_config('app.current_user_id', :uid, false)"),
            {"uid": str(goal.owner_id)},
        )
        with pytest.raises(Exception) as trigger_error:
            owner_conn.execute(
                sa_text("UPDATE supervisor_goal_lease_events SET worker_id = 'tampered' WHERE goal_id = :g"),
                {"g": str(goal.id)},
            )
        assert "append-only" in str(trigger_error.value)
        owner_conn.rollback()
        with pytest.raises(Exception) as delete_error:
            owner_conn.execute(
                sa_text("DELETE FROM supervisor_goal_lease_events WHERE goal_id = :g"), {"g": str(goal.id)}
            )
        assert "append-only" in str(delete_error.value)
        owner_conn.rollback()

    assert len(_events(db_session, goal)) == 1
    assert claim is not None


def _another_goal_with_envelope(db):
    """A second goal for the same owner, so a test can hold two leases from two identities."""
    _set_rls_user(db)
    goal = create_goal(
        db,
        owner_id=FOUNDER_USER_ID,
        title="lease restart other goal",
        original_instruction="edit a file",
        created_by="test",
    )
    proposal = propose_execution_scope(
        db,
        owner_id=FOUNDER_USER_ID,
        goal_id=goal.id,
        idempotency_key=f"lease-restart-proposal-{uuid.uuid4()}",
        proposed_paths=["README.md"],
        proposed_capabilities=["read_file"],
        proposed_risk="low",
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=FOUNDER_USER_ID,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"lease-restart-envelope-{uuid.uuid4()}",
    )
    db.commit()
    return goal, envelope


def test_a_restarted_process_releases_only_its_own_previous_leases(db_session, goal_with_envelope):
    """T1: a `kill -9` never reaches F7's release, so the goal stays leased for the TTL.

    On startup the new process releases the leases held by its OWN worker identity (it cannot
    have claimed any yet), writes the release to the append-only history, and leaves every
    other identity's lease exactly as it was -- that case is still bounded by the TTL, not
    guessed about.
    """
    goal_a, envelope_a = goal_with_envelope
    goal_b, envelope_b = _another_goal_with_envelope(db_session)

    claim_a = _claim(db_session, goal_a, envelope_a, worker="worker-a")
    claim_b = _claim(db_session, goal_b, envelope_b, worker="worker-b")
    db_session.commit()
    assert claim_a is not None and claim_b is not None

    released = release_supervisor_goal_leases_from_previous_process(db_session, worker_id="worker-a")
    db_session.commit()
    assert released == 1

    assert _lease_rows(db_session, goal_a)[0].status == "released"
    assert _lease_rows(db_session, goal_b)[0].status == "active"

    _set_rls_user(db_session)
    detail = db_session.execute(
        sa_text(
            "SELECT detail->>'reason' FROM supervisor_goal_lease_events "
            "WHERE goal_id = :g AND event_type = 'released'"
        ),
        {"g": str(goal_a.id)},
    ).scalar_one()
    assert detail == "worker_process_restarted"

    # The restarted process can immediately run this goal again; the other one still blocks it.
    assert _claim(db_session, goal_a, envelope_a, worker="worker-a") is not None
    assert _claim(db_session, goal_b, envelope_b, worker="worker-a") is None


def test_authorized_owner_erasure_removes_both_the_lease_and_its_history(db_session, goal_with_envelope):
    goal, envelope = goal_with_envelope
    claim = _claim(db_session, goal, envelope)
    assert claim is not None
    _release(db_session, claim)
    db_session.commit()
    _set_rls_user(db_session)
    db_session.execute(sa_text("SELECT erase_own_supervisor_goal_leases()"))
    db_session.commit()
    assert _lease_rows(db_session, goal) == []
    assert _events(db_session, goal) == []
