"""Priority 8 — Revoked Founder Session vs Account-Erasure Authority (executable reassessment).

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own
tests.conftest. This suite replaces the earlier LOW/informational dismissal with evidence.

Root mechanism (see _harness.py): account_erasure_receipt_session_current (migration 0086)
decides erasure session-binding with `rt.created_at > u.sessions_valid_after` — a comparison
of two STORED timestamps — and never inspects refresh_tokens.revoked_at. Every real revocation
primitive sets sessions_valid_after to the current second truncated DOWN (to match JWT integer
`iat`), and also sets revoked_at on the session it revokes. So a session whose refresh row was
created at a sub-second offset inside that same second stays `created_at > sessions_valid_after`
== TRUE after it is explicitly revoked, and the helper — ignoring revoked_at — still treats it
as current.

  ES1  (point 1) the REAL Founder password reset revokes the session, yet the same-second
       created_at leaves receipt_session_current TRUE.
  ES2  (point 2) the condition is wall-clock-independent: it persists at +1s / +1min / years
       later (pure stored-vs-stored); the different-second case is correctly FALSE (contrast).
  ES3  (points 3 & 6) end-to-end: under the revoked session a NEW erasure operation begins and
       an ACTUAL protected-data grant row is DELETED and committed.
  ES4  (point 4) which revocation mechanisms close the boundary: the access-jti blocklist
       (single logout / family-reuse revocation) DOES; the sessions_valid_after bump
       (reset / logout-all / revoke-sessions) does NOT in the same-second case.
  ES5  (point 5) an already-STARTED operation continues under the revoked session, and is NOT
       bounded by the reauth receipt lifetime (set_phase / delete succeed after receipt expiry).
  ES6  (point 7) the discrepancy survives a fresh DB session / process restart (committed rows).
  ES7  (remediation) every revocation sets revoked_at IS NOT NULL on the session, so the
       one-line fix `AND rt.revoked_at IS NULL` in the helper closes the window deterministically.

Finding: CONFIRMED DEFECT. Responsible component:
`account_erasure_receipt_session_current` (backend/alembic/versions/0086_account_erasure_reauth_binding.py).
Read-only examiner. RECALL STAYS INACTIVE. No paid calls; no production mutation. first_cycle_go=false.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    GRANTS,
    RECEIPTS,
    begin_operation,
    founder_with_session,
    grant_exists,
    insert_password_reset_token,
    make_reauth_receipt,
    mint_recall_grant,
    place_created_at,
    real_founder_password_reset,
    real_revoke_all_sessions,
    session_current,
    session_row,
    set_ctx,
    set_phase,
    set_session_timestamps,
)

_REVOKE_BLOCKED = "grants are revoked, not ordinarily deleted"


@pytest.fixture
def founder():
    return founder_with_session()


# ---------------------------------------------------------------------------
# ES1 (point 1) — the REAL Founder password reset revokes the session (revoked_at set,
#   sessions_valid_after bumped), yet a same-second created_at leaves the erasure
#   session-binding helper returning TRUE for that just-revoked session.
# ---------------------------------------------------------------------------
def test_ES1_same_second_reset_leaves_session_current_true(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]

    # drive the REAL reset_founder_password_with_token production function
    plaintext = uuid.uuid4().hex + uuid.uuid4().hex
    insert_password_reset_token(superuser_db, plaintext)
    real_founder_password_reset(db_session, plaintext)

    sva, created_at, revoked_at = session_row(superuser_db, jti)
    assert revoked_at is not None, "reset must revoke the refresh token"

    # model the same-second coincidence deterministically: the session's refresh row was created
    # 500ms into the very second the reset truncated sessions_valid_after down to.
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))

    # the just-revoked session STILL satisfies erasure session-binding
    set_ctx(db_session, owner, access_jti=jti)
    assert session_current(db_session, owner, jti) is True
    db_session.rollback()


# ---------------------------------------------------------------------------
# ES2 (point 2) — the condition is a function of stored values only, not the wall clock.
#   Pinning the stored timestamps years in the past keeps it TRUE (so it never "closes the
#   next second"); the ordinary different-second revoke is correctly FALSE (contrast).
# ---------------------------------------------------------------------------
def test_ES2_condition_is_wallclock_independent(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    base = datetime(2020, 1, 1, 0, 0, 30)

    # same-second: sessions_valid_after truncated to :30, token created at :30.500, revoked.
    set_session_timestamps(superuser_db, jti,
                           sessions_valid_after=base, created_at=base + timedelta(milliseconds=500),
                           revoked_at=base)
    set_ctx(db_session, owner, access_jti=jti)
    # true now, and still true on repeated evaluation "seconds/minutes/years later" — the stored
    # comparison does not move with the clock (these rows are already ~6 years old).
    assert session_current(db_session, owner, jti) is True
    assert session_current(db_session, owner, jti) is True
    db_session.rollback()

    # different-second (the ordinary revoke): sessions_valid_after a full second after created_at
    # -> created_at <= sessions_valid_after -> correctly FALSE. The bug is strictly the sub-second
    # same-second truncation, not a total failure of the check.
    set_session_timestamps(superuser_db, jti,
                           sessions_valid_after=base + timedelta(seconds=1),
                           created_at=base + timedelta(milliseconds=500), revoked_at=base)
    set_ctx(db_session, owner, access_jti=jti)
    assert session_current(db_session, owner, jti) is False
    db_session.rollback()


# ---------------------------------------------------------------------------
# ES3 (points 3 & 6) — END TO END: under a session the REAL reset just revoked, a NEW erasure
#   operation BEGINS and an ACTUAL protected-data grant row is DELETED and committed. The stale
#   authority authorizes real destruction, not merely a helper returning true.
# ---------------------------------------------------------------------------
def test_ES3_revoked_session_authorizes_real_protected_data_delete(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    grant = mint_recall_grant(owner, founder["authority"])
    receipt = make_reauth_receipt(owner, jti)              # minted while the session was live
    assert grant_exists(superuser_db, grant) is True

    # REAL password reset -> session revoked; model the same-second created_at placement.
    plaintext = uuid.uuid4().hex + uuid.uuid4().hex
    insert_password_reset_token(superuser_db, plaintext)
    real_founder_password_reset(db_session, plaintext)
    sva, _, revoked_at = session_row(superuser_db, jti)
    assert revoked_at is not None
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))

    # under the REVOKED session: begin a fresh erasure operation (point 3) ...
    set_ctx(db_session, owner, access_jti=jti)
    op = begin_operation(db_session, owner, receipt)
    set_phase(db_session, op, owner, "personal_recall_erasure")
    db_session.commit()

    # ... and actually DELETE the protected-data row and COMMIT it (point 6)
    set_ctx(db_session, owner, access_jti=jti, erasure=True, operation_id=op)
    assert db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant}).rowcount == 1
    db_session.commit()
    assert grant_exists(superuser_db, grant) is False       # protected data destroyed under a revoked session


# ---------------------------------------------------------------------------
# ES4 (point 4) — which revocation mechanisms actually close the erasure boundary.
#   The access-jti blocklist path (single logout, family-reuse revocation) DOES close it; the
#   sessions_valid_after bump path (reset / logout-all / revoke-sessions) does NOT, same-second.
# ---------------------------------------------------------------------------
def test_ES4_jti_blocklist_closes_but_bulk_revoke_does_not(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]

    # the REAL founder bulk revocation (revoke_all_sessions_for_user -> revoke_current_founder_sessions),
    # second-truncated, with a same-second created_at -> still current (does NOT close).
    set_ctx(db_session, owner, access_jti=jti)
    real_revoke_all_sessions(db_session, founder["refresh_plaintext"])
    sva, _, revoked_at = session_row(superuser_db, jti)
    assert revoked_at is not None
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))
    set_ctx(db_session, owner, access_jti=jti)
    assert session_current(db_session, owner, jti) is True   # bulk revoke left the boundary open

    # now additionally blocklist the access jti — the mechanism single-logout / family-reuse
    # revocation use (app.token_revocation.revoke_access_token). The helper honors THAT.
    from app.token_revocation import revoke_access_token
    revoke_access_token(db_session, jti)
    set_ctx(db_session, owner, access_jti=jti)
    assert session_current(db_session, owner, jti) is False  # jti blocklist closes it
    db_session.rollback()


# ---------------------------------------------------------------------------
# ES5 (point 5) — an already-STARTED operation continues under the revoked session, and is NOT
#   bounded by the reauth receipt lifetime: set_phase re-checks and a delete still succeeds even
#   after the receipt is expired, because the post-begin checks consult only session-currency +
#   consumed_at, never the receipt lifetime.
# ---------------------------------------------------------------------------
def test_ES5_in_flight_operation_continues_unbounded(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    grant = mint_recall_grant(owner, founder["authority"])
    receipt = make_reauth_receipt(owner, jti)

    # start the operation while the session is live
    set_ctx(db_session, owner, access_jti=jti)
    op = begin_operation(db_session, owner, receipt)
    set_phase(db_session, op, owner, "personal_recall_erasure")
    db_session.commit()

    # revoke the session (REAL reset, same-second) AND expire the consumed receipt
    plaintext = uuid.uuid4().hex + uuid.uuid4().hex
    insert_password_reset_token(superuser_db, plaintext)
    real_founder_password_reset(db_session, plaintext)
    sva, _, _ = session_row(superuser_db, jti)
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))
    superuser_db.execute(
        text(f"UPDATE {RECEIPTS} SET expires_at = now() - interval '1 hour', "
             f"issued_at = now() - interval '2 hours' WHERE receipt_id=:r"), {"r": receipt})
    superuser_db.commit()

    # the in-flight operation still deletes protected data despite revoked session + expired receipt
    set_ctx(db_session, owner, access_jti=jti, erasure=True, operation_id=op)
    assert db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant}).rowcount == 1
    db_session.commit()
    assert grant_exists(superuser_db, grant) is False


# ---------------------------------------------------------------------------
# ES6 (point 7) — the discrepancy is in committed rows, so it survives a process restart: a
#   brand-new backend session (no GUC / in-memory carryover) computes the helper TRUE for the
#   revoked session just as the original did.
# ---------------------------------------------------------------------------
def test_ES6_survives_fresh_db_session(db_session, superuser_db, founder):
    from app.db import migration_engine
    from sqlalchemy.orm import sessionmaker

    owner, jti = founder["owner"], founder["jti"]
    plaintext = uuid.uuid4().hex + uuid.uuid4().hex
    insert_password_reset_token(superuser_db, plaintext)
    real_founder_password_reset(db_session, plaintext)
    sva, _, revoked_at = session_row(superuser_db, jti)
    assert revoked_at is not None
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))

    fresh = sessionmaker(bind=migration_engine)()
    try:
        set_ctx(fresh, owner, access_jti=jti)
        assert session_current(fresh, owner, jti) is True
        fresh.rollback()
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# ES7 (remediation) — every real revocation sets refresh_tokens.revoked_at on the session it
#   revokes, so the helper's own ignored column is the deterministic fix point: an additional
#   `AND rt.revoked_at IS NULL` closes the window regardless of sub-second truncation. This test
#   proves revoked_at is populated (fix is applicable) and that gating on it flips the result.
# ---------------------------------------------------------------------------
def test_ES7_remediation_revoked_at_is_the_fix_point(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    plaintext = uuid.uuid4().hex + uuid.uuid4().hex
    insert_password_reset_token(superuser_db, plaintext)
    real_founder_password_reset(db_session, plaintext)
    sva, _, revoked_at = session_row(superuser_db, jti)
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))

    # the current (vulnerable) helper returns true ...
    set_ctx(db_session, owner, access_jti=jti)
    assert session_current(db_session, owner, jti) is True

    # ... while the proposed remediation predicate (the helper's EXISTS body PLUS
    # `rt.revoked_at IS NULL`) returns false on exactly the same stored rows. revoked_at being
    # non-null is what makes the one-line fix deterministic across every revocation path.
    assert revoked_at is not None
    remediated = db_session.execute(text(
        "SELECT EXISTS (SELECT 1 FROM users u "
        "JOIN refresh_tokens rt ON rt.user_id=u.id AND rt.access_jti=:j "
        "WHERE u.id=:o AND u.is_active IS TRUE "
        "AND NOT EXISTS (SELECT 1 FROM revoked_access_tokens rat WHERE rat.jti=:j) "
        "AND rt.created_at > u.sessions_valid_after "
        "AND rt.revoked_at IS NULL)"), {"j": jti, "o": str(owner)}).scalar_one()
    assert remediated is False
    db_session.rollback()
