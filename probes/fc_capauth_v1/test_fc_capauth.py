"""Founder Capability & Authority-Change core — independent adversarial examination (0094).

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own
tests.conftest, so every CHECK / guard / unique index / RLS / privilege under test is the REAL
production boundary. None of the 0094 guard messages exercised here is asserted anywhere in
tests/ (verified by grep).

  FC1  privilege escalation: a non-Founder cannot create an authority proposal (owner CHECK + RLS)
  FC2  the runtime cannot mint a capability grant or policy (SELECT-only)
  FC3  audit receipts are immutable and runtime cannot write them
  FC4  step-up receipts permit only one-way consumption
  FC5  capability grants permit only one terminal transition (no replay / no resurrection)
  FC6  one ACTIVE policy per capability (unique index) and policy authority fields are immutable
  FC7  authority proposals permit only one terminal transition
  FC8  owner isolation: a foreign owner cannot see Founder authority rows
  FC9  concurrent terminal transition on a grant -> exactly one wins
  FC10 replay: a duplicate grant idempotency_key is rejected by a unique constraint

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    AUDIT,
    GRANTS,
    POLICIES,
    PROPOSALS,
    ensure_founder,
    full_chain,
    new_audit,
    new_grant,
    new_policy,
    new_proposal,
    new_step_up,
    set_owner,
    status_of,
)


@pytest.fixture
def founder():
    return ensure_founder()


# ---------------------------------------------------------------------------
# FC1 — privilege escalation is blocked: a non-Founder cannot create an authority proposal.
# ---------------------------------------------------------------------------
def test_FC1_non_founder_cannot_create_proposal(db_session, founder, make_verified_user):
    other, _ = make_verified_user()

    # owner = attacker -> owner CHECK (= FOUNDER) rejects it
    set_owner(db_session, other.id)
    with pytest.raises(DBAPIError, match="ck_founder_proposal_owner"):
        db_session.execute(text(
            f"INSERT INTO {PROPOSALS} (id, owner_id, change_kind, requested_changes, proposal_digest, "
            "proposed_by, status) VALUES (gen_random_uuid(), :o, 'policy', '[{\"c\": 1}]'::jsonb, :dg, 'x', 'proposed')"),
            {"o": other.id, "dg": uuid.uuid4().hex})
    db_session.rollback()

    # owner = FOUNDER but acting as the attacker -> RLS WITH CHECK rejects it
    set_owner(db_session, other.id)
    with pytest.raises(DBAPIError, match="row-level security"):
        db_session.execute(text(
            f"INSERT INTO {PROPOSALS} (id, owner_id, change_kind, requested_changes, proposal_digest, "
            "proposed_by, status) VALUES (gen_random_uuid(), :o, 'policy', '[{\"c\": 1}]'::jsonb, :dg, 'x', 'proposed')"),
            {"o": founder, "dg": uuid.uuid4().hex})
    db_session.rollback()


# ---------------------------------------------------------------------------
# FC2 — the runtime cannot mint a capability grant or policy (SELECT-only privilege).
# ---------------------------------------------------------------------------
def test_FC2_runtime_cannot_mint_grant_or_policy(db_session, founder):
    set_owner(db_session, founder)
    with pytest.raises(DBAPIError):
        db_session.execute(text(
            f"INSERT INTO {GRANTS} (id, owner_id, proposal_id, step_up_receipt_id, proposal_digest, "
            "issuer_signature, idempotency_key, expires_at) VALUES (gen_random_uuid(), :o, "
            "gen_random_uuid(), gen_random_uuid(), :d, :d, :d, now()+interval '5 min')"),
            {"o": founder, "d": uuid.uuid4().hex})
    db_session.rollback()

    set_owner(db_session, founder)
    with pytest.raises(DBAPIError):
        db_session.execute(text(
            f"INSERT INTO {POLICIES} (id, owner_id, capability_key, decision, version, authorized_by, "
            "grant_id, policy_digest, issuer_signature) VALUES (gen_random_uuid(), :o, 'cap', 'allow', 1, "
            "'x', gen_random_uuid(), :d, :d)"),
            {"o": founder, "d": uuid.uuid4().hex})
    db_session.rollback()


# ---------------------------------------------------------------------------
# FC3 — audit receipts are immutable and the runtime cannot write them.
# ---------------------------------------------------------------------------
def test_FC3_audit_receipts_immutable(db_session, superuser_db, founder):
    aid, _ = new_audit(superuser_db, founder)

    set_owner(db_session, founder)
    with pytest.raises(DBAPIError):                       # runtime: no INSERT privilege
        db_session.execute(text(
            f"INSERT INTO {AUDIT} (id, owner_id, event_type, subject_kind, subject_id, actor, "
            "receipt_digest, issuer_signature) VALUES (gen_random_uuid(), :o, 'e', 'k', 's', 'a', :d, :d)"),
            {"o": founder, "d": uuid.uuid4().hex})
    db_session.rollback()

    with pytest.raises(DBAPIError, match="Founder authority audit receipts are immutable"):
        superuser_db.execute(text(f"UPDATE {AUDIT} SET event_type='tampered' WHERE id=:i"), {"i": aid})
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# FC4 — step-up receipts permit only one-way consumption.
# ---------------------------------------------------------------------------
def test_FC4_step_up_one_way_consumption(superuser_db, founder):
    from _harness import STEP_UPS

    # consuming (null -> set), nothing else changed, is allowed
    sid = new_step_up(superuser_db, founder, consumed=False)
    superuser_db.execute(text(f"UPDATE {STEP_UPS} SET consumed_at=now() WHERE id=:i"), {"i": sid})
    superuser_db.commit()
    assert status_of(superuser_db, STEP_UPS, sid, "consumed_at") is not None

    # changing another field (even while consuming) is rejected
    sid2 = new_step_up(superuser_db, founder, consumed=False)
    with pytest.raises(DBAPIError, match="step-up receipts permit only one-way consumption"):
        superuser_db.execute(
            text(f"UPDATE {STEP_UPS} SET consumed_at=now(), purpose='changed' WHERE id=:i"), {"i": sid2})
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# FC5 — capability grants permit only one terminal transition (no replay / no resurrection).
# ---------------------------------------------------------------------------
def test_FC5_grant_one_terminal_transition(superuser_db, founder):
    c = full_chain(superuser_db, founder)
    grant = c["grant"]

    # active -> consumed (bindings unchanged) is allowed
    superuser_db.execute(
        text(f"UPDATE {GRANTS} SET status='consumed', consumed_at=now() WHERE id=:i"), {"i": grant})
    superuser_db.commit()
    assert status_of(superuser_db, GRANTS, grant) == "consumed"

    # a second transition off the (now terminal) state is rejected
    with pytest.raises(DBAPIError, match="capability grants permit only one terminal transition"):
        superuser_db.execute(text(f"UPDATE {GRANTS} SET status='cancelled' WHERE id=:i"), {"i": grant})
    superuser_db.rollback()

    # transitioning a fresh grant while mutating a binding is rejected
    c2 = full_chain(superuser_db, founder)
    with pytest.raises(DBAPIError, match="capability grants permit only one terminal transition"):
        superuser_db.execute(
            text(f"UPDATE {GRANTS} SET status='consumed', scope=jsonb_build_object('x', 1) WHERE id=:i"),
            {"i": c2["grant"]})
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# FC6 — one ACTIVE policy per capability (partial unique index); authority fields immutable.
# ---------------------------------------------------------------------------
def test_FC6_policy_single_active_and_immutable(superuser_db, founder):
    c = full_chain(superuser_db, founder, capability_key="mainai.push", version=1)
    policy = c["policy"]

    # a second ACTIVE policy for the same capability is rejected by the partial unique index
    with pytest.raises(IntegrityError):
        new_policy(superuser_db, founder, c["grant"], capability_key="mainai.push", version=2)
    superuser_db.rollback()

    # active -> superseded (unchanged) is allowed; mutating a field is rejected
    with pytest.raises(DBAPIError, match="capability policy authority fields are immutable"):
        superuser_db.execute(
            text(f"UPDATE {POLICIES} SET status='superseded', decision='deny' WHERE id=:i"), {"i": policy})
    superuser_db.rollback()
    superuser_db.execute(text(f"UPDATE {POLICIES} SET status='superseded' WHERE id=:i"), {"i": policy})
    superuser_db.commit()
    assert status_of(superuser_db, POLICIES, policy) == "superseded"


# ---------------------------------------------------------------------------
# FC7 — authority proposals permit only one terminal transition.
# ---------------------------------------------------------------------------
def test_FC7_proposal_one_terminal_transition(superuser_db, founder):
    prop = new_proposal(superuser_db, founder)

    # proposed -> applied (unchanged) is allowed
    superuser_db.execute(
        text(f"UPDATE {PROPOSALS} SET status='applied', authorized_at=now() WHERE id=:i"), {"i": prop})
    superuser_db.commit()
    assert status_of(superuser_db, PROPOSALS, prop) == "applied"

    # a second transition off 'applied' is rejected
    with pytest.raises(DBAPIError, match="authority proposals permit only one terminal transition"):
        superuser_db.execute(text(f"UPDATE {PROPOSALS} SET status='rejected' WHERE id=:i"), {"i": prop})
    superuser_db.rollback()

    # transitioning a fresh proposal while mutating a binding is rejected
    prop2 = new_proposal(superuser_db, founder)
    with pytest.raises(DBAPIError, match="authority proposals permit only one terminal transition"):
        superuser_db.execute(
            text(f"UPDATE {PROPOSALS} SET status='applied', change_kind='upgrade' WHERE id=:i"), {"i": prop2})
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# FC8 — owner isolation: a foreign owner cannot see Founder authority rows.
# ---------------------------------------------------------------------------
def test_FC8_owner_isolation(db_session, superuser_db, founder, make_verified_user):
    c = full_chain(superuser_db, founder)
    other, _ = make_verified_user()

    set_owner(db_session, other.id)
    for table, rid in ((PROPOSALS, c["proposal"]), (GRANTS, c["grant"]), (POLICIES, c["policy"])):
        assert db_session.execute(
            text(f"SELECT count(*) FROM {table} WHERE id=:i"), {"i": rid}).scalar_one() == 0, table


# ---------------------------------------------------------------------------
# FC9 — concurrent terminal transition on a grant: exactly one wins.
# ---------------------------------------------------------------------------
def test_FC9_concurrent_grant_terminal_transition(superuser_db, founder):
    from app.db import migration_engine

    c = full_chain(superuser_db, founder)
    grant = c["grant"]
    Session = sessionmaker(bind=migration_engine)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def consume():
        barrier.wait()
        db = Session()
        try:
            db.execute(text(f"UPDATE {GRANTS} SET status='consumed', consumed_at=now() WHERE id=:i"),
                       {"i": grant})
            db.commit()
            with lock:
                outcomes.append("ok")
        except DBAPIError as exc:
            db.rollback()
            with lock:
                outcomes.append("terminal" if "one terminal transition" in str(exc) else f"other:{exc}")
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for f in [pool.submit(consume) for _ in range(2)]:
            f.result(timeout=30)
    assert sorted(outcomes) == ["ok", "terminal"], outcomes
    assert status_of(superuser_db, GRANTS, grant) == "consumed"


# ---------------------------------------------------------------------------
# FC10 — replay: a duplicate grant idempotency_key is rejected by a unique constraint.
# ---------------------------------------------------------------------------
def test_FC10_duplicate_grant_idempotency_key_rejected(superuser_db, founder):
    prop = new_proposal(superuser_db, founder)
    step = new_step_up(superuser_db, founder)
    idem = uuid.uuid4().hex
    new_grant(superuser_db, founder, prop, step, idempotency_key=idem)
    with pytest.raises(IntegrityError, match="uq_founder_capability_grant_idem|duplicate key"):
        new_grant(superuser_db, founder, prop, step, idempotency_key=idem)
    superuser_db.rollback()
