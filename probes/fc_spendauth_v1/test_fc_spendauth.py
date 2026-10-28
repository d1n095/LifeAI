"""Provider Spend Database Authority — independent adversarial examination.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298 (latest examined lineage). DB-backed via
the target's own tests.conftest (alembic upgrade head + apply_mainai_execution_privileges),
so every guard/trigger/grant/RLS/function under test is the REAL production boundary.

Scope discipline: does NOT re-run the previously completed SPEND-STOP gate (the fact that a
STOP cascade revokes spend authorizations) nor the provider-spend HTTP API test. It targets
the unexamined DB-level effect boundary of `security_kernel_spend_guard`, the SECURITY
DEFINER settle/release paths, RLS owner isolation, concurrent reservation atomicity, and
transition/audit consistency. None of the guard's invariant messages is asserted anywhere in
the product test tree today.

  SP1  ceiling is a DB backstop even for a direct runtime over-reservation
  SP2  Founder bindings/ceilings are immutable (runtime column-denied AND guard-blocked)
  SP3  committed spend counters are monotonic (cannot decrease)
  SP4  runtime cannot directly release a reservation; only the audited SECURITY DEFINER path can
  SP5  after a STOP (which revokes the grant + emits one audit event) the runtime cannot
       resurrect it and cannot reserve against it — fail closed
  SP6  spend authority + usage ledger are append-only (runtime-denied AND guard/trigger-blocked)
  SP7  a forged/attacker owner context cannot reach another owner's grant (RLS isolation)
  SP8  concurrent reservations cannot jointly exceed the Founder ceiling (atomic under row lock)
  SP9  settle is atomic under rollback (counters + usage row unchanged; committed settle moves
       the hold reserved->spent)
  SP10 replay/idempotency: a repeated reserve does not double-hold; settle is idempotent; a
       released or settled hold fails closed on re-use

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    SPEND_TABLE,
    USAGE_TABLE,
    auth_row,
    authorize_spend,
    init_owner_epoch,
    make_goal_and_envelope,
    owner_stop,
    reserve,
    set_runtime_owner,
)


def _setup(superuser_db, make_verified_user, **kw):
    owner, _ = make_verified_user()
    goal, envelope = make_goal_and_envelope(superuser_db, owner.id)
    auth = authorize_spend(superuser_db, owner.id, goal.id, envelope.id, **kw)
    superuser_db.commit()
    return owner, goal, envelope, auth


# ---------------------------------------------------------------------------
# SP1 — ceiling is a DB backstop: a direct runtime UPDATE that pushes spent+reserved over the
#        Founder ceiling is refused by the guard, independent of the Python reserve checks.
# ---------------------------------------------------------------------------
def test_SP1_ceiling_is_a_db_backstop(db_session, superuser_db, make_verified_user):
    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user,
                                         max_cost_usd="10.00", max_requests=5)
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError, match="reservation exceeds Founder ceiling"):
        db_session.execute(
            text(f"UPDATE {SPEND_TABLE} SET reserved_cost_usd=:v WHERE id=:id"),
            {"v": Decimal("10.01"), "id": auth.id},
        )
    db_session.rollback()

    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError, match="reservation exceeds Founder ceiling"):
        db_session.execute(
            text(f"UPDATE {SPEND_TABLE} SET reserved_requests=:v WHERE id=:id"),
            {"v": 6, "id": auth.id},
        )
    db_session.rollback()

    row = auth_row(superuser_db, auth.id)
    assert row["reserved_cost_usd"] == Decimal("0") and row["reserved_requests"] == 0


# ---------------------------------------------------------------------------
# SP2 — Founder ceilings are immutable: the runtime is denied the column at the privilege
#        layer, and even a privileged writer is refused by the guard.
# ---------------------------------------------------------------------------
def test_SP2_ceilings_are_immutable(db_session, superuser_db, make_verified_user):
    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user, max_cost_usd="10.00")

    # (a) runtime has no grant on max_cost_usd -> permission denied before the guard
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError):
        db_session.execute(
            text(f"UPDATE {SPEND_TABLE} SET max_cost_usd=max_cost_usd*2 WHERE id=:id"),
            {"id": auth.id},
        )
    db_session.rollback()

    # (b) even the schema-owner writer is refused by the guard
    with pytest.raises(DBAPIError, match="bindings and ceilings are immutable"):
        superuser_db.execute(
            text(f"UPDATE {SPEND_TABLE} SET max_cost_usd=max_cost_usd*2 WHERE id=:id"),
            {"id": auth.id},
        )
    superuser_db.rollback()

    assert auth_row(superuser_db, auth.id)["max_cost_usd"] == Decimal("10.00")


# ---------------------------------------------------------------------------
# SP3 — committed spend counters are monotonic: they may rise (within ceiling) but never fall.
# ---------------------------------------------------------------------------
def test_SP3_committed_counters_are_monotonic(db_session, superuser_db, make_verified_user):
    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user, max_cost_usd="10.00")

    # raise committed spend within the ceiling (allowed)
    set_runtime_owner(db_session, owner.id)
    db_session.execute(
        text(f"UPDATE {SPEND_TABLE} SET spent_cost_usd=:v WHERE id=:id"),
        {"v": Decimal("2.00"), "id": auth.id},
    )
    db_session.commit()
    assert auth_row(superuser_db, auth.id)["spent_cost_usd"] == Decimal("2.00")

    # attempt to lower it (forbidden)
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError, match="committed spend counters cannot decrease"):
        db_session.execute(
            text(f"UPDATE {SPEND_TABLE} SET spent_cost_usd=:v WHERE id=:id"),
            {"v": Decimal("1.00"), "id": auth.id},
        )
    db_session.rollback()
    assert auth_row(superuser_db, auth.id)["spent_cost_usd"] == Decimal("2.00")


# ---------------------------------------------------------------------------
# SP4 — the runtime cannot directly release a reservation hold; only the audited SECURITY
#        DEFINER release path may lower reserved_*.
# ---------------------------------------------------------------------------
def test_SP4_runtime_cannot_release_only_audited_path_can(db_session, superuser_db, make_verified_user):
    from app.provider_spend.service import release_provider_spend_call

    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user,
                                         max_cost_usd="10.00", per_request="1.00")
    source_ref = f"call:{uuid.uuid4()}"
    reserve(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=source_ref)
    superuser_db.commit()
    assert auth_row(superuser_db, auth.id)["reserved_cost_usd"] == Decimal("1.00")

    # direct runtime release attempt -> guarded
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError, match="runtime cannot directly release reserved spend"):
        db_session.execute(
            text(f"UPDATE {SPEND_TABLE} SET reserved_cost_usd=reserved_cost_usd-:d WHERE id=:id"),
            {"d": Decimal("1.00"), "id": auth.id},
        )
    db_session.rollback()
    assert auth_row(superuser_db, auth.id)["reserved_cost_usd"] == Decimal("1.00")

    # the audited SECURITY DEFINER path legitimately frees the hold
    release_provider_spend_call(superuser_db, owner_id=owner.id, source_ref=source_ref)
    superuser_db.commit()
    assert auth_row(superuser_db, auth.id)["reserved_cost_usd"] == Decimal("0")


# ---------------------------------------------------------------------------
# SP5 — a STOP revokes the grant (with exactly one audit event); the runtime can neither
#        resurrect it nor reserve against it afterwards (fail closed).
# ---------------------------------------------------------------------------
def test_SP5_stop_revoke_cannot_be_resurrected_or_reused(db_session, superuser_db, make_verified_user):
    from app.provider_spend.service import ProviderSpendError, reserve_provider_spend_call

    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user, max_cost_usd="10.00")

    owner_stop(superuser_db, owner.id)
    superuser_db.commit()
    assert auth_row(superuser_db, auth.id)["status"] == "revoked"   # STOP cascade (not re-tested here)

    # exactly one audit event for this grant's revocation transition
    assert superuser_db.execute(
        text("SELECT count(*) FROM security_kernel_authority_events "
             "WHERE event_type='provider_spend_revoked' AND subject_id=:id"),
        {"id": str(auth.id)},
    ).scalar_one() == 1

    # runtime cannot resurrect the revoked grant
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError, match="provider spend authority cannot be resurrected"):
        db_session.execute(
            text(f"UPDATE {SPEND_TABLE} SET status='active' WHERE id=:id"),
            {"id": auth.id},
        )
    db_session.rollback()

    # and cannot reserve against it through the service (no live authorization / envelope)
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(ProviderSpendError):
        reserve_provider_spend_call(
            db_session, owner_id=owner.id, goal_id=goal.id,
            source_ref=f"post-stop:{uuid.uuid4()}", provider="openai", model="gpt-4o",
            hold_prompt_tokens=0, hold_completion_tokens=0,
        )
    db_session.rollback()
    assert auth_row(superuser_db, auth.id)["status"] == "revoked"


# ---------------------------------------------------------------------------
# SP6 — spend authority and the usage ledger are append-only: the runtime is denied at the
#        privilege layer, and a privileged writer is refused by the trigger.
# ---------------------------------------------------------------------------
def test_SP6_spend_authority_and_usage_ledger_are_append_only(db_session, superuser_db, make_verified_user):
    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user)
    source_ref = f"call:{uuid.uuid4()}"
    reserve(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=source_ref)
    superuser_db.commit()

    # (a) runtime cannot DELETE a spend authorization (privilege revoked)
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError):
        db_session.execute(text(f"DELETE FROM {SPEND_TABLE} WHERE id=:id"), {"id": auth.id})
    db_session.rollback()

    # (b) privileged DELETE without the erasure GUC is refused by the trigger
    with pytest.raises(DBAPIError, match="spend authority history is immutable"):
        superuser_db.execute(text(f"DELETE FROM {SPEND_TABLE} WHERE id=:id"), {"id": auth.id})
    superuser_db.rollback()

    # (c) runtime cannot mutate the append-only usage ledger (UPDATE privilege revoked)
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError):
        db_session.execute(
            text(f"UPDATE {USAGE_TABLE} SET cost_usd=cost_usd+1 WHERE source_ref=:s"),
            {"s": source_ref},
        )
    db_session.rollback()

    # (d) privileged UPDATE outside the settle/release GUC is refused by the trigger
    with pytest.raises(DBAPIError, match="append-only"):
        superuser_db.execute(
            text(f"UPDATE {USAGE_TABLE} SET cost_usd=cost_usd+1 WHERE source_ref=:s"),
            {"s": source_ref},
        )
    superuser_db.rollback()

    assert auth_row(superuser_db, auth.id)["status"] == "active"


# ---------------------------------------------------------------------------
# SP7 — RLS owner isolation: a forged/attacker owner context cannot see or mutate another
#        owner's spend authorization (the UPDATE matches zero rows, no escalation).
# ---------------------------------------------------------------------------
def test_SP7_forged_owner_context_cannot_cross_owners(db_session, superuser_db, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _, env_a = make_goal_and_envelope(superuser_db, owner_a.id)
    goal_b, env_b = make_goal_and_envelope(superuser_db, owner_b.id)
    auth_b = authorize_spend(superuser_db, owner_b.id, goal_b.id, env_b.id, max_cost_usd="10.00")
    superuser_db.commit()

    # attacker authenticates as A, targets B's grant id directly
    set_runtime_owner(db_session, owner_a.id)
    result = db_session.execute(
        text(f"UPDATE {SPEND_TABLE} SET reserved_cost_usd=reserved_cost_usd+1 WHERE id=:id"),
        {"id": auth_b.id},
    )
    assert result.rowcount == 0           # RLS hid B's row from A entirely
    db_session.commit()

    row = auth_row(superuser_db, auth_b.id)
    assert row["reserved_cost_usd"] == Decimal("0") and row["status"] == "active"


# ---------------------------------------------------------------------------
# SP8 — concurrent reservations cannot jointly exceed the Founder ceiling. Two writers each
#        add a hold that fits alone but not together; the row lock serializes them and the
#        guard refuses the one that would overflow. Exactly one commits.
# ---------------------------------------------------------------------------
def test_SP8_concurrent_reservations_cannot_exceed_ceiling(superuser_db, make_verified_user):
    from app.db import migration_engine

    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user, max_cost_usd="10.00")
    auth_id = auth.id
    Session = sessionmaker(bind=migration_engine)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def add_hold():
        barrier.wait()
        db = Session()
        try:
            db.execute(
                text(f"UPDATE {SPEND_TABLE} SET reserved_cost_usd=reserved_cost_usd+:d WHERE id=:id"),
                {"d": Decimal("6.00"), "id": auth_id},
            )
            db.commit()
            with lock:
                outcomes.append("ok")
        except DBAPIError as exc:
            db.rollback()
            with lock:
                outcomes.append("ceiling" if "reservation exceeds Founder ceiling" in str(exc) else f"other:{exc}")
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(add_hold) for _ in range(2)]
        for f in futs:
            f.result(timeout=30)

    assert sorted(outcomes) == ["ceiling", "ok"], outcomes
    final = auth_row(superuser_db, auth_id)
    assert final["reserved_cost_usd"] == Decimal("6.00")              # exactly one hold applied
    assert final["reserved_cost_usd"] <= final["max_cost_usd"]


# ---------------------------------------------------------------------------
# SP9 — settle is atomic under rollback: an aborted settle leaves the hold and the usage row
#        exactly as they were; a committed settle moves the hold from reserved to spent.
# ---------------------------------------------------------------------------
def test_SP9_settle_is_atomic_under_rollback(superuser_db, make_verified_user):
    from app.db import migration_engine
    from app.provider_spend.service import settle_provider_spend_call

    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user,
                                         max_cost_usd="10.00", per_request="1.00")
    source_ref = f"call:{uuid.uuid4()}"
    reserve(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=source_ref)
    superuser_db.commit()
    before = auth_row(superuser_db, auth.id)
    assert before["reserved_cost_usd"] == Decimal("1.00") and before["spent_cost_usd"] == Decimal("0")

    Session = sessionmaker(bind=migration_engine)

    # rolled-back settle: in-txn effect is real, but nothing commits
    tx = Session()
    try:
        settle_provider_spend_call(tx, owner_id=owner.id, source_ref=source_ref,
                                   cost_usd=Decimal("0.50"))
        in_tx = tx.execute(
            text(f"SELECT reserved_cost_usd, spent_cost_usd FROM {SPEND_TABLE} WHERE id=:id"),
            {"id": auth.id},
        ).mappings().one()
        assert in_tx["spent_cost_usd"] == Decimal("0.50")      # settle applied inside the txn
        tx.rollback()
    finally:
        tx.close()

    superuser_db.rollback()
    after_rollback = auth_row(superuser_db, auth.id)
    assert after_rollback["reserved_cost_usd"] == Decimal("1.00")   # hold intact
    assert after_rollback["spent_cost_usd"] == Decimal("0")         # nothing committed
    assert superuser_db.execute(
        text(f"SELECT status FROM {USAGE_TABLE} WHERE source_ref=:s"), {"s": source_ref},
    ).scalar_one() == "reserved"

    # committed settle moves the hold reserved -> spent
    tx2 = Session()
    try:
        settle_provider_spend_call(tx2, owner_id=owner.id, source_ref=source_ref,
                                   cost_usd=Decimal("0.50"))
        tx2.commit()
    finally:
        tx2.close()
    superuser_db.rollback()
    final = auth_row(superuser_db, auth.id)
    assert final["spent_cost_usd"] == Decimal("0.50")
    assert final["reserved_cost_usd"] == Decimal("0")               # hold released into actuals
    assert superuser_db.execute(
        text(f"SELECT status FROM {USAGE_TABLE} WHERE source_ref=:s"), {"s": source_ref},
    ).scalar_one() == "settled"


# ---------------------------------------------------------------------------
# SP10 — replay / idempotency: a repeated reserve does not double-hold; settle is idempotent;
#         a settled or released hold fails closed on re-use under the same source_ref.
# ---------------------------------------------------------------------------
def test_SP10_reserve_settle_replay_is_idempotent_and_fails_closed(superuser_db, make_verified_user):
    from app.provider_spend.service import (
        ProviderSpendError,
        release_provider_spend_call,
        reserve_provider_spend_call,
        settle_provider_spend_call,
    )

    owner, goal, envelope, auth = _setup(superuser_db, make_verified_user,
                                         max_cost_usd="10.00", per_request="1.00")

    # reserve twice under the same source_ref -> one hold only
    sref = f"call:{uuid.uuid4()}"
    _, created1 = reserve(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=sref)
    _, created2 = reserve(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=sref)
    superuser_db.commit()
    assert created1 is True and created2 is False
    assert auth_row(superuser_db, auth.id)["reserved_cost_usd"] == Decimal("1.00")  # not doubled

    # settle, then settle again (idempotent, no extra spend)
    settle_provider_spend_call(superuser_db, owner_id=owner.id, source_ref=sref, cost_usd=Decimal("0.40"))
    superuser_db.commit()
    spent_once = auth_row(superuser_db, auth.id)["spent_cost_usd"]
    settle_provider_spend_call(superuser_db, owner_id=owner.id, source_ref=sref, cost_usd=Decimal("0.40"))
    superuser_db.commit()
    assert auth_row(superuser_db, auth.id)["spent_cost_usd"] == spent_once   # idempotent

    # releasing an already-settled hold fails closed
    with pytest.raises(DBAPIError, match="cannot release an already-settled"):
        release_provider_spend_call(superuser_db, owner_id=owner.id, source_ref=sref)
    superuser_db.rollback()

    # a brand-new reserve, released, then re-used under the same source_ref fails closed
    sref2 = f"call:{uuid.uuid4()}"
    reserve(superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=sref2)
    superuser_db.commit()
    release_provider_spend_call(superuser_db, owner_id=owner.id, source_ref=sref2)
    superuser_db.commit()
    with pytest.raises(ProviderSpendError, match="already released"):
        reserve_provider_spend_call(
            superuser_db, owner_id=owner.id, goal_id=goal.id, source_ref=sref2,
            provider="openai", model="gpt-4o", hold_prompt_tokens=0, hold_completion_tokens=0,
        )
    superuser_db.rollback()
