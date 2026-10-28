"""Effect-Specific Database Authority — independent adversarial examination of migration 0099.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298 (frozen parent 078312d7; backend
byte-identical to fully-tested e8d6fc8). DB-backed via the target's own tests.conftest, which
runs `alembic upgrade head` + apply_mainai_execution_privileges, so every REVOKE/GRANT and
every trigger under test is the REAL production boundary on this exact SHA.

Scope discipline: this suite deliberately does NOT re-run the builder's four proofs
(runtime-denied forged-GUC UPDATE, cross-owner + stale-owner-epoch rejection, concurrent
idempotency + append-only immutability, reject-after-STOP) nor the already-certified STOP-clear
and FA-PRIV behaviours. It targets the untested boundary:

  EA1  table-level (not merely column-level) UPDATE denial for the runtime role
  EA2  defense-in-depth: the SECURITY DEFINER body refuses a runtime LOGIN role even if a
       future GRANT re-exposed EXECUTE (the exact scenario the migration comment claims)
  EA3  incomplete / malformed binding fails closed inside the DB function
  EA4  GLOBAL authority-epoch staleness is fenced (builder covered the OWNER epoch)
  EA5  transition <-> audit atomicity: a rolled-back revoke leaves NO audit event and NO
       transition; a committed revoke leaves exactly one of each
  EA6  deterministic TOCTOU close: a concurrent STOP that advances the epoch while a revoke
       is in flight forces the revoke stale under the function's FOR UPDATE re-read
  EA7  compatibility: runtime privileges after 0099 coexist with the certified FA-PRIV/STOP
       controls and the append-only audit triggers (no regression, no silent widening)

Read-only examiner. No paid calls, no production mutation, loopback-only (egress guard).
first_cycle_go=false.
"""
from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    AUDIT_TABLE,
    ENVELOPE_TABLE,
    REVOKE_FN,
    STOP_FN,
    envelope_status,
    init_owner_epoch,
    make_active_envelope,
    revoke_audit_count,
    revoke_fn,
    set_runtime_owner,
)


# ---------------------------------------------------------------------------
# EA1 — the runtime role lost UPDATE on the WHOLE envelope table, not just the three
#        terminal-transition columns it formerly held (migration 0099 + rls.py broadening).
# ---------------------------------------------------------------------------
def test_EA1_runtime_role_has_no_envelope_update_at_all(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    envelope = make_active_envelope(superuser_db, owner.id)
    superuser_db.commit()

    # (a) a formerly-GRANTed transition column is now denied at the privilege layer
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError):
        db_session.execute(
            text(f"UPDATE {ENVELOPE_TABLE} SET revocation_reason='forged' WHERE id=:id"),
            {"id": envelope.id},
        )
    db_session.rollback()

    # (b) a column that was NEVER grantable is denied too — proving a table-level REVOKE,
    #     i.e. the runtime cannot touch the row through any column path.
    set_runtime_owner(db_session, owner.id)
    with pytest.raises(DBAPIError):
        db_session.execute(
            text(f"UPDATE {ENVELOPE_TABLE} SET goal_id=goal_id WHERE id=:id"),
            {"id": envelope.id},
        )
    db_session.rollback()

    assert envelope_status(superuser_db, envelope.id) == "active"


# ---------------------------------------------------------------------------
# EA2 — defense in depth: even if a future GRANT re-exposed EXECUTE to mainai_app, the
#        function body still refuses a runtime LOGIN role (session_user='mainai_app').
#        This is the exact claim in the migration's own comment. We prove it by temporarily
#        granting EXECUTE (restored in finally) and calling through the real runtime session.
# ---------------------------------------------------------------------------
def test_EA2_body_refuses_runtime_login_even_with_execute_granted(
    db_session, superuser_db, make_verified_user
):
    owner, _ = make_verified_user()
    envelope = make_active_envelope(superuser_db, owner.id)
    global_epoch, owner_epoch = init_owner_epoch(superuser_db, owner.id)
    superuser_db.commit()

    granted = False
    try:
        superuser_db.execute(text(f"GRANT EXECUTE ON FUNCTION {REVOKE_FN} TO mainai_app"))
        superuser_db.commit()
        granted = True

        set_runtime_owner(db_session, owner.id)
        with pytest.raises(DBAPIError) as excinfo:
            revoke_fn(db_session, owner.id, envelope.id, global_epoch, owner_epoch,
                      "runtime via hypothetical future grant")
        db_session.rollback()
        assert "runtime role cannot revoke execution authority" in str(excinfo.value)
    finally:
        if granted:
            superuser_db.execute(text(f"REVOKE EXECUTE ON FUNCTION {REVOKE_FN} FROM mainai_app"))
            superuser_db.commit()

    # the defense-in-depth refusal left the envelope untouched
    assert envelope_status(superuser_db, envelope.id) == "active"
    # and EXECUTE is back to denied (restoration held)
    assert superuser_db.execute(
        text(f"SELECT has_function_privilege('mainai_app','{REVOKE_FN}','EXECUTE')")
    ).scalar_one() is False


# ---------------------------------------------------------------------------
# EA3 — incomplete / malformed binding fails closed INSIDE the privileged function
#        (NULL ids, negative epoch, empty/over-long reason). Builder covered cross-owner and
#        stale-owner-epoch, not these input guards.
# ---------------------------------------------------------------------------
def test_EA3_incomplete_or_malformed_binding_fails_closed(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    envelope = make_active_envelope(superuser_db, owner.id)
    global_epoch, owner_epoch = init_owner_epoch(superuser_db, owner.id)
    superuser_db.commit()

    cases = [
        # (owner, envelope, g_epoch, o_epoch, reason, expected message fragment)
        (owner.id, None, global_epoch, owner_epoch, "null envelope",
         "complete execution-revocation binding is required"),
        (None, envelope.id, global_epoch, owner_epoch, "null owner",
         "complete execution-revocation binding is required"),
        (owner.id, envelope.id, -1, owner_epoch, "negative global epoch",
         "complete execution-revocation binding is required"),
        (owner.id, envelope.id, global_epoch, owner_epoch, "   ",
         "revocation reason must contain 1..1024 characters"),
        (owner.id, envelope.id, global_epoch, owner_epoch, "x" * 1025,
         "revocation reason must contain 1..1024 characters"),
    ]
    for o, e, g, oe, reason, fragment in cases:
        with pytest.raises(DBAPIError) as excinfo:
            revoke_fn(superuser_db, o, e, g, oe, reason)
        superuser_db.rollback()
        assert fragment in str(excinfo.value), (reason, str(excinfo.value))

    assert envelope_status(superuser_db, envelope.id) == "active"


# ---------------------------------------------------------------------------
# EA4 — GLOBAL authority-epoch staleness is fenced (builder tested the OWNER epoch).
# ---------------------------------------------------------------------------
def test_EA4_global_epoch_staleness_is_fenced(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    envelope = make_active_envelope(superuser_db, owner.id)
    global_epoch, owner_epoch = init_owner_epoch(superuser_db, owner.id)
    superuser_db.commit()

    with pytest.raises(DBAPIError, match="authority epoch is stale"):
        revoke_fn(superuser_db, owner.id, envelope.id, global_epoch + 1, owner_epoch,
                  "stale global epoch")
    superuser_db.rollback()
    assert envelope_status(superuser_db, envelope.id) == "active"


# ---------------------------------------------------------------------------
# EA5 — transition and audit event are atomically coupled: a rolled-back revoke persists
#        NEITHER a status transition NOR an audit row; a committed revoke persists exactly one
#        of each. (The audit row is written by the AFTER-UPDATE trigger in the same txn.)
# ---------------------------------------------------------------------------
def test_EA5_transition_and_audit_are_atomic_under_rollback(superuser_db, make_verified_user):
    from app.db import migration_engine

    owner, _ = make_verified_user()
    envelope = make_active_envelope(superuser_db, owner.id)
    global_epoch, owner_epoch = init_owner_epoch(superuser_db, owner.id)
    envelope_id = envelope.id
    owner_id = owner.id
    superuser_db.commit()

    Session = sessionmaker(bind=migration_engine)

    # ---- rolled-back attempt: in-transaction effect is visible, but nothing commits ----
    tx = Session()
    try:
        result = revoke_fn(tx, owner_id, envelope_id, global_epoch, owner_epoch, "atomic probe")
        assert result == envelope_id
        in_tx_status = tx.execute(
            text(f"SELECT status FROM {ENVELOPE_TABLE} WHERE id=:id"), {"id": envelope_id}
        ).scalar_one()
        in_tx_audit = tx.execute(
            text(f"SELECT count(*) FROM {AUDIT_TABLE} "
                 "WHERE event_type='execution_envelope_revoked' AND subject_id=:id"),
            {"id": str(envelope_id)},
        ).scalar_one()
        assert in_tx_status == "revoked"   # transition is real inside the txn
        assert in_tx_audit == 1            # audit written by the same-txn trigger
        tx.rollback()                      # operator/route aborts before commit
    finally:
        tx.close()

    superuser_db.rollback()  # fresh snapshot
    assert envelope_status(superuser_db, envelope_id) == "active"      # no half-transition
    assert revoke_audit_count(superuser_db, envelope_id) == 0          # no orphan audit event

    # ---- committed attempt: exactly one transition and exactly one audit event ----
    tx2 = Session()
    try:
        assert revoke_fn(tx2, owner_id, envelope_id, global_epoch, owner_epoch,
                         "committed revoke") == envelope_id
        tx2.commit()
    finally:
        tx2.close()

    superuser_db.rollback()
    assert envelope_status(superuser_db, envelope_id) == "revoked"
    assert revoke_audit_count(superuser_db, envelope_id) == 1


# ---------------------------------------------------------------------------
# EA6 — deterministic TOCTOU close under a concurrent STOP.
#
#        A real owner STOP is the only legitimate way the owner epoch advances (the epoch
#        guard rejects a bare bump), and it atomically cascade-revokes the owner's active
#        envelope (migration 0098). We hold that STOP's owner-epoch row lock uncommitted while
#        a Founder revoke — carrying the PRE-STOP epoch the service read in Python — is in
#        flight. The revoke blocks on the function's `FOR UPDATE` re-read, then, once the STOP
#        commits, sees the advanced epoch and fails stale.
#
#        Property proven: a concurrent STOP vs. an in-flight Founder revoke produces EXACTLY
#        ONE envelope transition and EXACTLY ONE audit event (the STOP's cascade); the racing
#        revoke fails closed (stale) under the lock re-read and adds neither a second
#        transition nor a duplicate audit row. The read-then-act window is closed — the
#        service's Python-side epoch read can never silently act under superseded authority.
# ---------------------------------------------------------------------------
def test_EA6_concurrent_stop_closes_toctou_window(superuser_db, make_verified_user):
    from app.db import migration_engine

    owner, _ = make_verified_user()
    envelope = make_active_envelope(superuser_db, owner.id)
    global_epoch, owner_epoch = init_owner_epoch(superuser_db, owner.id)
    envelope_id = envelope.id
    owner_id = owner.id
    superuser_db.commit()

    Session = sessionmaker(bind=migration_engine)

    # Thread A: begin an owner STOP that advances the epoch and HOLD the row lock uncommitted.
    a = Session()
    a.execute(
        text(
            "UPDATE workforce_authority_epoch SET stopped=true, epoch=epoch+1, "
            "reason='examiner concurrent STOP', activated_at=clock_timestamp() "
            "WHERE scope_key=:scope"
        ),
        {"scope": str(owner_id)},
    )  # not committed -> owner-epoch row write-locked

    outcome: dict = {}

    def revoke_racing_stop():
        b = Session()
        try:
            b.execute(text("SET statement_timeout='20s'"))
            revoke_fn(b, owner_id, envelope_id, global_epoch, owner_epoch, "revoke racing STOP")
            b.commit()
            outcome["result"] = "revoked"
        except Exception as exc:  # noqa: BLE001 — we classify below
            b.rollback()
            outcome["result"] = "error"
            outcome["message"] = str(exc)
        finally:
            b.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(revoke_racing_stop)
            time.sleep(1.5)
            # B must be genuinely blocked on the owner-epoch FOR UPDATE lock A holds.
            assert not future.done(), "revoke did not block on the owner-epoch lock (TOCTOU window open)"
            a.commit()   # release the lock with the ADVANCED epoch
            future.result(timeout=25)
    finally:
        a.close()

    assert outcome.get("result") == "error", outcome
    assert "authority epoch is stale" in outcome.get("message", ""), outcome
    # Exactly one transition + one audit event total — contributed by the STOP cascade, not
    # the racing revoke, which failed closed and added nothing.
    superuser_db.rollback()
    assert envelope_status(superuser_db, envelope_id) == "revoked"       # by the STOP cascade
    assert revoke_audit_count(superuser_db, envelope_id) == 1            # not 2: no double-act


# ---------------------------------------------------------------------------
# EA7 — compatibility: after migration 0099, the runtime role's privilege surface coexists
#        with the previously-certified FA-PRIV/STOP controls and the append-only audit
#        triggers. No regression (old controls still enforced) and no silent widening.
# ---------------------------------------------------------------------------
def test_EA7_runtime_privileges_compatible_with_prior_controls(superuser_db):
    def has_table(priv, table=ENVELOPE_TABLE):
        return superuser_db.execute(
            text(f"SELECT has_table_privilege('mainai_app','{table}','{priv}')")
        ).scalar_one()

    def has_fn(sig):
        return superuser_db.execute(
            text(f"SELECT has_function_privilege('mainai_app','{sig}','EXECUTE')")
        ).scalar_one()

    def trigger_present(name):
        return superuser_db.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname=:n AND NOT tgisinternal"),
            {"n": name},
        ).scalar_one() == 1

    # --- the new effect-specific boundary (0099) ---
    assert has_table("UPDATE") is False           # table-level UPDATE revoked from runtime
    assert has_fn(REVOKE_FN) is False             # new privileged op not runtime-executable

    # --- previously-certified controls still enforced (NOT re-run, only confirmed present) ---
    assert has_fn(STOP_FN) is False               # FA-PRIV/STOP op still runtime-denied

    # --- append-only audit visibility + immutability unchanged ---
    assert has_table("SELECT", AUDIT_TABLE) is True    # runtime may still read the audit trail
    assert has_table("UPDATE", AUDIT_TABLE) is False   # but cannot mutate it
    assert has_table("DELETE", AUDIT_TABLE) is False

    # --- the triggers that make the boundary real are all still installed ---
    assert trigger_present("execution_authorization_envelopes_audit")      # writes the audit row
    assert trigger_present("execution_authorization_envelopes_db_boundary")  # one-way transition guard
    assert trigger_present("security_kernel_events_immutable")             # audit append-only
