"""Cross-Boundary Authorization Consistency — independent adversarial examination.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own
tests.conftest. Focus (per the assignment): interactions that could RESTORE previously-revoked
authority or permit execution using STALE credentials — across Founder session validity,
authorization epochs, worker/effect authorization, revocation of active privileged operations,
and durable recovery.

Scope / non-duplication: this does NOT re-run the concurrent-STOP TOCTOU or the effect-specific
authority guards (fc_effauth/fc_spendauth/fc_transportauth), nor the authenticated STOP-clear
machinery itself (certified elsewhere). It examines the CROSS-boundary consistency: a STOP→clear
epoch cycle must not resurrect revoked effect authority; a stale/ revoked Founder session must
not drive a privileged operation; a revoked envelope must not be consumable by a transport
effect; and a "recovered" (fresh-session) read must reflect revocation.

  CB1  a STOP-revoked execution envelope cannot be restored by ANY writer (so no later clear can)
  CB2  a STOP-revoked provider-spend authorization cannot be restored by ANY writer
  CB3  a STOP-cancelled push grant is terminal and cannot be restored by ANY writer
  CB4  a Founder credential change (session invalidation) makes a prior erasure authority stale
  CB5  a revoked execution envelope cannot be consumed by a transport (push) effect
  CB6  durable recovery: a fresh session after STOP sees revoked authority, not a stale snapshot

Note on STOP→clear: advancing the epoch out of STOP requires an authenticated STOP-clear
authorization even for the schema owner (confirmed empirically), and the clear transition writes
ONLY the epoch row — there is no effect-table write and no re-activation cascade. Combined with
CB1–CB3 (active-resurrection is categorically refused for every writer), this establishes that a
STOP→clear cycle cannot restore any previously-revoked effect authority, without re-testing the
certified authenticated-clear control itself.

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    ENVELOPE_TABLE,
    SPEND_TABLE,
    build_authority_graph,
    envelope_status,
    erase_begin,
    founder_session,
    make_erase_reauth_receipt,
    make_push_grant,
    push_status,
    set_runtime_owner,
    spend_status,
    stop_owner,
)


@pytest.fixture
def graph(superuser_db, make_verified_user):
    return build_authority_graph(superuser_db, make_verified_user)


# ---------------------------------------------------------------------------
# CB1 — a STOP-revoked execution envelope cannot be restored by ANY writer (runtime is
#        privilege-denied; a privileged writer is refused by the one-way guard) — so no later
#        epoch clear, which writes only the epoch row, can bring it back.
# ---------------------------------------------------------------------------
def test_CB1_revoked_envelope_cannot_be_restored(db_session, superuser_db, graph):
    owner = graph["user"].id
    env = graph["envelope"].id
    assert envelope_status(superuser_db, env) == "active"

    stop_owner(superuser_db, owner)
    assert envelope_status(superuser_db, env) == "revoked"      # STOP cascade revoked it

    set_runtime_owner(db_session, owner)
    with pytest.raises(DBAPIError):                             # runtime: no UPDATE privilege path
        db_session.execute(text(f"UPDATE {ENVELOPE_TABLE} SET status='active' WHERE id=:i"), {"i": env})
    db_session.rollback()

    with pytest.raises(DBAPIError):                             # privileged writer: guard refuses
        superuser_db.execute(text(f"UPDATE {ENVELOPE_TABLE} SET status='active' WHERE id=:i"), {"i": env})
    superuser_db.rollback()
    assert envelope_status(superuser_db, env) == "revoked"


# ---------------------------------------------------------------------------
# CB2 — a STOP-revoked provider-spend authorization cannot be restored by ANY writer.
# ---------------------------------------------------------------------------
def test_CB2_revoked_spend_cannot_be_restored(db_session, superuser_db, graph):
    owner = graph["user"].id
    spend = graph["spend"].id
    assert spend_status(superuser_db, spend) == "active"

    stop_owner(superuser_db, owner)
    assert spend_status(superuser_db, spend) == "revoked"

    set_runtime_owner(db_session, owner)
    with pytest.raises(DBAPIError):
        db_session.execute(text(f"UPDATE {SPEND_TABLE} SET status='active' WHERE id=:i"), {"i": spend})
    db_session.rollback()

    with pytest.raises(DBAPIError, match="cannot be resurrected"):
        superuser_db.execute(text(f"UPDATE {SPEND_TABLE} SET status='active' WHERE id=:i"), {"i": spend})
    superuser_db.rollback()
    assert spend_status(superuser_db, spend) == "revoked"


# ---------------------------------------------------------------------------
# CB3 — a STOP-cancelled push grant is terminal and cannot be restored by ANY writer.
# ---------------------------------------------------------------------------
def test_CB3_cancelled_push_grant_cannot_be_restored(db_session, superuser_db, graph):
    owner = graph["user"].id
    push = make_push_grant(superuser_db, graph)
    assert push_status(superuser_db, push.id) == "active"

    stop_owner(superuser_db, owner)
    assert push_status(superuser_db, push.id) == "cancelled"    # STOP cascade cancelled it

    set_runtime_owner(db_session, owner)
    with pytest.raises(DBAPIError):
        db_session.execute(text("UPDATE mainai_push_grants SET status='active' WHERE id=:i"), {"i": push.id})
    db_session.rollback()

    with pytest.raises(DBAPIError, match="terminal push grant state is immutable"):
        superuser_db.execute(text("UPDATE mainai_push_grants SET status='active' WHERE id=:i"), {"i": push.id})
    superuser_db.rollback()
    assert push_status(superuser_db, push.id) == "cancelled"


# ---------------------------------------------------------------------------
# CB4 — a Founder credential change (session invalidation) makes a previously-issued erasure
#        reauth authority STALE: an operation cannot begin under the now-revoked session.
# ---------------------------------------------------------------------------
def test_CB4_credential_change_invalidates_prior_erasure_authority(db_session, superuser_db):
    owner, jti = founder_session()
    receipt = make_erase_reauth_receipt(owner, jti)     # valid while the session is live

    # A Founder session invalidation (logout-all / password reset / revoke-sessions all bump
    # sessions_valid_after). Advance it clearly past the session's refresh-token creation so the
    # prior session is unambiguously stale (see the sub-second observation in the verdict).
    superuser_db.execute(
        text("UPDATE users SET sessions_valid_after = now() + interval '1 minute' WHERE id=:i"),
        {"i": owner})
    superuser_db.commit()

    # beginning an erasure operation under the now-stale session is refused
    with pytest.raises(DBAPIError, match="not bound to a current session|requires current owner context"):
        erase_begin(db_session, owner, receipt, jti)
    db_session.rollback()


# ---------------------------------------------------------------------------
# CB5 — after a STOP, a previously-valid transport (push) effect cannot be consumed: the STOP
#        cascade both cancels the grant and revokes its execution envelope, so the consume path
#        fails closed rather than applying an effect under revoked authority.
# ---------------------------------------------------------------------------
def test_CB5_stop_blocks_push_effect_consume(superuser_db, graph):
    from app.mainai_execution.activation_security import ActivationAuthorityError, consume_push_grant
    from _harness import BASE, LOCAL

    owner = graph["user"].id
    push = make_push_grant(superuser_db, graph)

    stop_owner(superuser_db, owner)
    assert envelope_status(superuser_db, graph["envelope"].id) == "revoked"
    assert push_status(superuser_db, push.id) == "cancelled"

    # the transport effect cannot execute under the revoked authority (grant cancelled and/or
    # envelope superseded) -- ActivationEnvelopeSuperseded subclasses ActivationAuthorityError
    with pytest.raises(ActivationAuthorityError):
        consume_push_grant(superuser_db, owner_id=owner, grant_id=push.id, job_id=graph["job"].id,
                           local_commit_sha=LOCAL, observed_base_sha=BASE, authority_version=7)
    superuser_db.rollback()
    assert push_status(superuser_db, push.id) == "cancelled"   # not consumed


# ---------------------------------------------------------------------------
# CB6 — durable recovery: a fresh session opened AFTER a STOP (simulating a process restart /
#        recovery) observes the revoked authority, not a pre-STOP snapshot, and cannot act on it.
# ---------------------------------------------------------------------------
def test_CB6_recovery_reflects_revocation(superuser_db, graph):
    from app.db import migration_engine
    from sqlalchemy.orm import sessionmaker

    owner = graph["user"].id
    env = graph["envelope"].id
    spend = graph["spend"].id

    stop_owner(superuser_db, owner)

    # a brand-new session (no in-memory state carried over) reads the committed, revoked truth
    recovered = sessionmaker(bind=migration_engine)()
    try:
        assert recovered.execute(
            text(f"SELECT status FROM {ENVELOPE_TABLE} WHERE id=:i"), {"i": env}).scalar_one() == "revoked"
        assert recovered.execute(
            text(f"SELECT status FROM {SPEND_TABLE} WHERE id=:i"), {"i": spend}).scalar_one() == "revoked"
        # and the recovered session cannot resurrect either
        with pytest.raises(DBAPIError):
            recovered.execute(text(f"UPDATE {ENVELOPE_TABLE} SET status='active' WHERE id=:i"), {"i": env})
        recovered.rollback()
    finally:
        recovered.close()
