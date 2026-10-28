"""Account-Erasure Operation Authority — independent adversarial examination (0085/0086).

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own
tests.conftest, so every guard / SECURITY DEFINER function / privilege / RLS under test is the
REAL production boundary. The authority substrate (founder session + owner key + a Recall
grant to delete) is built through the product's own privileged services.

RECALL STAYS INACTIVE (no ingestion/retrieval). None of the erasure guard messages exercised
here is asserted anywhere in tests/ (verified by grep). Defects, if any, are recorded
separately from intended privileged behaviour.

  AE1  reauth receipts are governed: runtime cannot write them (privilege-denied AND guard)
  AE2  operations are governed: runtime cannot fabricate a fake active erasure op (dual layer)
  AE3  a FORGED erasure GUC + a random operation id cannot unlock a protected-data delete
  AE4  begin_operation validates the reauth receipt (missing / already-consumed / wrong session)
  AE5  the reauth receipt is single-use under concurrency (exactly one operation begins)
  AE6  cross-owner: a receipt/op for one owner cannot authorize erasing another owner's data
  AE7  phase transitions are bounded (unsupported phase / wrong owner context rejected)
  AE8  a protected-data delete is unlocked ONLY in the exact authorized phase; a mid-phase
       rollback leaves the operation resumable (transaction boundary / recovery)
  AE9  losing the founder session mid-operation re-closes the gate (fail-closed)
  AE10 completion closes the gate and preserves the operation + consumed receipt (audit)

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    GRANTS,
    OPERATIONS,
    RECEIPTS,
    begin_operation,
    complete_operation,
    founder_with_session,
    grant_exists,
    make_reauth_receipt,
    mint_recall_grant,
    set_ctx,
    set_phase,
)

_REVOKE_BLOCKED = "grants are revoked, not ordinarily deleted"


@pytest.fixture
def founder():
    owner, jti, authority = founder_with_session()
    return {"owner": owner, "jti": jti, "authority": authority}


# ---------------------------------------------------------------------------
# AE1 — reauth receipts are governed: the runtime cannot write them.
# ---------------------------------------------------------------------------
def test_AE1_reauth_receipts_are_governed(db_session, superuser_db, founder):
    owner = founder["owner"]
    set_ctx(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError):                       # SELECT-only: no INSERT privilege
        db_session.execute(
            text(f"INSERT INTO {RECEIPTS} (receipt_id, owner_id, access_jti, purpose, issued_at, expires_at) "
                 "VALUES (gen_random_uuid(), :o, 'j', 'ACCOUNT_ERASURE', now(), now()+interval '1 min')"),
            {"o": owner})
    db_session.rollback()

    granted = False
    try:
        superuser_db.execute(text(f"GRANT INSERT ON {RECEIPTS} TO mainai_app"))
        superuser_db.commit()
        granted = True
        set_ctx(db_session, owner, access_jti=founder["jti"])
        with pytest.raises(DBAPIError, match="receipts are governed; raw runtime writes are forbidden"):
            db_session.execute(
                text(f"INSERT INTO {RECEIPTS} (receipt_id, owner_id, access_jti, purpose, issued_at, expires_at) "
                     "VALUES (gen_random_uuid(), :o, 'j', 'ACCOUNT_ERASURE', now(), now()+interval '1 min')"),
                {"o": owner})
        db_session.rollback()
    finally:
        if granted:
            superuser_db.execute(text(f"REVOKE INSERT ON {RECEIPTS} FROM mainai_app"))
            superuser_db.commit()


# ---------------------------------------------------------------------------
# AE2 — operations are governed: the runtime cannot fabricate a fake ACTIVE erasure op (which
#        is exactly what would otherwise unlock every phase-gated delete).
# ---------------------------------------------------------------------------
def test_AE2_operations_are_governed(db_session, superuser_db, founder):
    owner = founder["owner"]
    set_ctx(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError):                       # SELECT-only post-0086
        db_session.execute(
            text(f"INSERT INTO {OPERATIONS} (operation_id, owner_id, status, phase) "
                 "VALUES (gen_random_uuid(), :o, 'active', 'personal_recall_erasure')"),
            {"o": owner})
    db_session.rollback()

    granted = False
    try:
        superuser_db.execute(text(f"GRANT INSERT ON {OPERATIONS} TO mainai_app"))
        superuser_db.commit()
        granted = True
        set_ctx(db_session, owner, access_jti=founder["jti"])
        with pytest.raises(DBAPIError, match="operations are governed; use account erasure functions"):
            db_session.execute(
                text(f"INSERT INTO {OPERATIONS} (operation_id, owner_id, status, phase) "
                     "VALUES (gen_random_uuid(), :o, 'active', 'personal_recall_erasure')"),
                {"o": owner})
        db_session.rollback()
    finally:
        if granted:
            superuser_db.execute(text(f"REVOKE INSERT ON {OPERATIONS} FROM mainai_app"))
            superuser_db.commit()


# ---------------------------------------------------------------------------
# AE3 — a forged erasure GUC + a random operation id cannot unlock a protected-data delete.
# ---------------------------------------------------------------------------
def test_AE3_forged_erasure_guc_cannot_unlock_delete(db_session, superuser_db, founder):
    owner = founder["owner"]
    grant = mint_recall_grant(owner, founder["authority"])

    # a real founder session, erasure flag on, but a fabricated operation id (no op row)
    set_ctx(db_session, owner, access_jti=founder["jti"], erasure=True, operation_id=uuid.uuid4())
    with pytest.raises(DBAPIError, match=_REVOKE_BLOCKED):
        db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant})
    db_session.rollback()
    assert grant_exists(superuser_db, grant) is True


# ---------------------------------------------------------------------------
# AE4 — begin_operation validates the reauth receipt: missing, already-consumed, wrong session.
# ---------------------------------------------------------------------------
def test_AE4_begin_operation_validates_receipt(db_session, founder):
    owner = founder["owner"]

    set_ctx(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="missing or wrong owner"):
        begin_operation(db_session, owner, uuid.uuid4())
    db_session.rollback()

    # wrong session: a real receipt, but the current session JTI does not match it
    receipt = make_reauth_receipt(owner, founder["jti"])
    set_ctx(db_session, owner, access_jti=str(uuid.uuid4()))
    with pytest.raises(DBAPIError, match="not bound to a current session"):
        begin_operation(db_session, owner, receipt)
    db_session.rollback()

    # already consumed: begin once, then again with the same receipt
    set_ctx(db_session, owner, access_jti=founder["jti"])
    begin_operation(db_session, owner, receipt)
    db_session.commit()
    set_ctx(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="already consumed"):
        begin_operation(db_session, owner, receipt)
    db_session.rollback()


# ---------------------------------------------------------------------------
# AE5 — the reauth receipt is single-use under concurrency: two racing begins -> one op only.
# ---------------------------------------------------------------------------
def test_AE5_reauth_receipt_single_use_under_concurrency(founder):
    from app.db import engine  # the restricted mainai_app engine

    owner = founder["owner"]
    jti = founder["jti"]
    receipt = make_reauth_receipt(owner, jti)
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def begin_once():
        barrier.wait()
        db = Session()
        try:
            set_ctx(db, owner, access_jti=jti)
            begin_operation(db, owner, receipt)
            db.commit()
            with lock:
                outcomes.append("began")
        except DBAPIError as exc:
            db.rollback()
            with lock:
                outcomes.append("consumed" if ("already consumed" in str(exc) or "concurrently consumed" in str(exc)) else f"other:{exc}")
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for f in [pool.submit(begin_once) for _ in range(2)]:
            f.result(timeout=30)
    assert sorted(outcomes) == ["began", "consumed"], outcomes


# ---------------------------------------------------------------------------
# AE6 — cross-owner: a receipt/op for one owner cannot authorize erasing another owner's data.
# ---------------------------------------------------------------------------
def test_AE6_cross_owner_cannot_erase(db_session, superuser_db, founder, make_verified_user):
    owner = founder["owner"]
    grant = mint_recall_grant(owner, founder["authority"])
    other, _ = make_verified_user()
    receipt = make_reauth_receipt(owner, founder["jti"])

    # begin for a different owner using the founder's receipt -> rejected
    set_ctx(db_session, other.id, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="missing or wrong owner|requires current owner context"):
        begin_operation(db_session, other.id, receipt)
    db_session.rollback()

    # a legitimate founder op, but an attacker session (other owner) cannot reach the grant
    set_ctx(db_session, owner, access_jti=founder["jti"])
    op = begin_operation(db_session, owner, receipt)
    set_phase(db_session, op, owner, "personal_recall_erasure")
    db_session.commit()
    set_ctx(db_session, other.id, access_jti=founder["jti"], erasure=True, operation_id=op)
    res = db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant})
    assert res.rowcount == 0                              # RLS + owner-bound erasure_current
    db_session.commit()
    assert grant_exists(superuser_db, grant) is True


# ---------------------------------------------------------------------------
# AE7 — phase transitions are bounded: unsupported phase and wrong owner context are rejected.
# ---------------------------------------------------------------------------
def test_AE7_phase_transitions_are_bounded(db_session, founder, make_verified_user):
    owner = founder["owner"]
    receipt = make_reauth_receipt(owner, founder["jti"])
    set_ctx(db_session, owner, access_jti=founder["jti"])
    op = begin_operation(db_session, owner, receipt)
    db_session.commit()

    set_ctx(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="unsupported account erasure phase"):
        set_phase(db_session, op, owner, "arbitrary_phase")
    db_session.rollback()

    other, _ = make_verified_user()
    set_ctx(db_session, other.id, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="requires current owner context"):
        set_phase(db_session, op, owner, "personal_recall_erasure")
    db_session.rollback()

    # ordering is enforced: cannot skip 'started' -> 'personal_data_erasure'
    set_ctx(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="phase transition is invalid or lacks current reauth authority"):
        set_phase(db_session, op, owner, "personal_data_erasure")
    db_session.rollback()


# ---------------------------------------------------------------------------
# AE8 — a protected-data delete is unlocked ONLY in the exact authorized phase; a mid-phase
#        rollback leaves the operation resumable (transaction boundary / recovery).
# ---------------------------------------------------------------------------
def test_AE8_delete_only_in_exact_phase_and_rollback_is_resumable(db_session, superuser_db, founder):
    owner = founder["owner"]
    grant = mint_recall_grant(owner, founder["authority"])
    receipt = make_reauth_receipt(owner, founder["jti"])
    set_ctx(db_session, owner, access_jti=founder["jti"])
    op = begin_operation(db_session, owner, receipt)      # phase 'started'
    db_session.commit()

    # wrong phase ('started') -> delete blocked
    set_ctx(db_session, owner, access_jti=founder["jti"], erasure=True, operation_id=op)
    with pytest.raises(DBAPIError, match=_REVOKE_BLOCKED):
        db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant})
    db_session.rollback()
    assert grant_exists(superuser_db, grant) is True

    # advance to the exact phase
    set_ctx(db_session, owner, access_jti=founder["jti"])
    set_phase(db_session, op, owner, "personal_recall_erasure")
    db_session.commit()

    # delete then ROLLBACK -> grant survives, operation still active & resumable
    set_ctx(db_session, owner, access_jti=founder["jti"], erasure=True, operation_id=op)
    assert db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant}).rowcount == 1
    db_session.rollback()
    assert grant_exists(superuser_db, grant) is True      # rollback undid the delete
    assert superuser_db.execute(
        text(f"SELECT status||'/'||phase FROM {OPERATIONS} WHERE operation_id=:op"),
        {"op": op}).scalar_one() == "active/personal_recall_erasure"

    # resume: redo the delete and commit
    set_ctx(db_session, owner, access_jti=founder["jti"], erasure=True, operation_id=op)
    assert db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant}).rowcount == 1
    db_session.commit()
    assert grant_exists(superuser_db, grant) is False


# ---------------------------------------------------------------------------
# AE9 — losing the founder session mid-operation re-closes the gate (fail-closed).
# ---------------------------------------------------------------------------
def test_AE9_session_loss_recloses_gate(db_session, superuser_db, founder):
    from app.founder import FOUNDER_USER_ID

    owner = founder["owner"]
    grant = mint_recall_grant(owner, founder["authority"])
    receipt = make_reauth_receipt(owner, founder["jti"])
    set_ctx(db_session, owner, access_jti=founder["jti"])
    op = begin_operation(db_session, owner, receipt)
    set_phase(db_session, op, owner, "personal_recall_erasure")
    db_session.commit()

    # revoke the founder session: push sessions_valid_after ahead of the refresh token
    superuser_db.execute(
        text("UPDATE users SET sessions_valid_after = now() + interval '1 minute' WHERE id=:i"),
        {"i": FOUNDER_USER_ID})
    superuser_db.commit()

    set_ctx(db_session, owner, access_jti=founder["jti"], erasure=True, operation_id=op)
    with pytest.raises(DBAPIError, match=_REVOKE_BLOCKED):
        db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant})
    db_session.rollback()
    assert grant_exists(superuser_db, grant) is True


# ---------------------------------------------------------------------------
# AE10 — completion closes the gate and preserves the operation + consumed receipt (audit).
# ---------------------------------------------------------------------------
def test_AE10_completion_closes_gate_and_preserves_record(db_session, superuser_db, founder):
    owner = founder["owner"]
    grant = mint_recall_grant(owner, founder["authority"])
    receipt = make_reauth_receipt(owner, founder["jti"])
    set_ctx(db_session, owner, access_jti=founder["jti"])
    op = begin_operation(db_session, owner, receipt)
    set_phase(db_session, op, owner, "personal_recall_erasure")
    set_phase(db_session, op, owner, "personal_data_erasure")   # ordered progression (0088)
    complete_operation(db_session, op, owner)
    db_session.commit()

    # gate closed: status no longer 'active'
    set_ctx(db_session, owner, access_jti=founder["jti"], erasure=True, operation_id=op)
    with pytest.raises(DBAPIError, match=_REVOKE_BLOCKED):
        db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant})
    db_session.rollback()
    assert grant_exists(superuser_db, grant) is True

    # the operation and its consumed receipt are preserved as a durable audit record
    assert superuser_db.execute(
        text(f"SELECT status||'/'||phase FROM {OPERATIONS} WHERE operation_id=:op"),
        {"op": op}).scalar_one() == "completed/completed"
    assert superuser_db.execute(
        text(f"SELECT consumed_by_operation_id FROM {RECEIPTS} WHERE receipt_id=:r"),
        {"r": receipt}).scalar_one() == op
