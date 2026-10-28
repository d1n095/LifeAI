"""Priority 9 — Independent Regression Gate: consistent session-revocation + timestamp
semantics across Founder authority, account erasure, and privileged session-gated effects.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own tests.conftest.

Invariant under gate: a session that any real revocation primitive has invalidated (revoked_at
set, or sessions_valid_after bumped, or expired) must NOT be treated as current by ANY
session-gated authority helper, independent of sub-second truncation.

  REQUIRED-BEHAVIOUR tests (xfail(strict), keyed to the confirmed Priority-8 defect) — they fail
  on the broken build (reported as expected-fail) and flip to a loud XPASS->failure the moment
  the one-line fix lands, which is the signal to drop the marker and enforce the gate:
    RG1  account erasure: a session the REAL password reset just revoked (same second) is rejected
    RG2  account erasure: an EXPIRED refresh session is rejected
    RG3  account erasure: a session with refresh_tokens.revoked_at set is rejected

  POSITIVE-INVARIANT locks (plain PASS) — the correct pattern the codebase already ships, guarded
  against regression:
    RG4  Founder authority: personal_recall_founder_authority_current rejects the same-second
         reset-revoked session (it already checks revoked_at)
    RG5  Founder authority: personal_recall_founder_authority_current rejects an EXPIRED session
    RG6  privileged effect: revoke_current_founder_sessions (the session-gated privileged
         revocation effect) refuses a revoked/expired session ('live canonical Founder session required')

Effect authorization for envelope/spend/push transport is STOP-epoch / grant-state gated (it does
NOT use the session-timestamp pattern) and its revoked-stays-revoked behaviour is certified by
fc_effauth / fc_crossauth; it is not re-tested here.

Read-only examiner. RECALL INACTIVE. No paid calls; no production mutation. first_cycle_go=false.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    P8_DEFECT,
    erasure_session_current,
    founder_with_session,
    insert_password_reset_token,
    place_created_at,
    real_founder_password_reset,
    recall_founder_authority_current,
    session_row,
    set_ctx,
    set_token_fields,
)


@pytest.fixture
def founder():
    return founder_with_session()


def _reset_same_second(db_session, superuser_db, jti):
    """Drive the REAL reset, then place created_at 500ms into the truncated second (models the
    same-second coincidence deterministically). Returns the post-reset sessions_valid_after."""
    plaintext = uuid.uuid4().hex + uuid.uuid4().hex
    insert_password_reset_token(superuser_db, plaintext)
    real_founder_password_reset(db_session, plaintext)
    sva, _, revoked_at, _ = session_row(superuser_db, jti)
    assert revoked_at is not None
    place_created_at(superuser_db, jti, sva + timedelta(milliseconds=500))
    return sva


# ===== REQUIRED behaviour — account erasure (xfail strict until the fix lands) ==============

@pytest.mark.xfail(strict=True, reason=P8_DEFECT)
def test_RG1_erasure_rejects_same_second_revoked_session(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    _reset_same_second(db_session, superuser_db, jti)
    set_ctx(db_session, owner, access_jti=jti)
    # REQUIRED: a revoked session is not current. (Broken build returns True -> xfail.)
    assert erasure_session_current(db_session, owner, jti) is False
    db_session.rollback()


@pytest.mark.xfail(strict=True, reason=P8_DEFECT)
def test_RG2_erasure_rejects_expired_session(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    # live-by-sessions_valid_after but EXPIRED refresh token
    set_token_fields(superuser_db, jti, expires_at=datetime.utcnow() - timedelta(hours=1))
    set_ctx(db_session, owner, access_jti=jti)
    # REQUIRED: an expired session is not current. (Helper ignores expires_at -> True -> xfail.)
    assert erasure_session_current(db_session, owner, jti) is False
    db_session.rollback()


@pytest.mark.xfail(strict=True, reason=P8_DEFECT)
def test_RG3_erasure_rejects_explicitly_revoked_token(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    # explicit revoked_at with created_at still ahead of sessions_valid_after
    set_token_fields(superuser_db, jti, revoked_at=datetime.utcnow())
    set_ctx(db_session, owner, access_jti=jti)
    # REQUIRED: an explicitly revoked token is not current. (Helper ignores revoked_at -> xfail.)
    assert erasure_session_current(db_session, owner, jti) is False
    db_session.rollback()


# ===== POSITIVE invariant locks — the correct pattern already in the codebase ===============

def test_RG4_founder_authority_rejects_same_second_revoked_session(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    _reset_same_second(db_session, superuser_db, jti)
    # the Recall founder-authority helper checks revoked_at, so it correctly rejects it NOW
    set_ctx(db_session, owner, recall_founder_jti=jti)
    assert recall_founder_authority_current(db_session, owner) is False
    db_session.rollback()


def test_RG5_founder_authority_rejects_expired_session(db_session, superuser_db, founder):
    owner, jti = founder["owner"], founder["jti"]
    set_token_fields(superuser_db, jti, expires_at=datetime.utcnow() - timedelta(hours=1))
    set_ctx(db_session, owner, recall_founder_jti=jti)
    assert recall_founder_authority_current(db_session, owner) is False
    db_session.rollback()


def test_RG6_privileged_effect_refuses_revoked_session(db_session, superuser_db, founder):
    """The session-gated privileged effect revoke_current_founder_sessions (0095) requires a LIVE
    session (revoked_at IS NULL AND expires_at>now AND created_at>sessions_valid_after AND not
    jti-blocklisted). A revoked session cannot invoke it. This is the correct, consistent
    timestamp/revocation semantics the erasure helper must match."""
    owner, jti = founder["owner"], founder["jti"]
    set_token_fields(superuser_db, jti, revoked_at=datetime.utcnow())
    set_ctx(db_session, owner, access_jti=jti)
    with pytest.raises(DBAPIError, match="live canonical Founder session required"):
        db_session.execute(text("SELECT revoke_current_founder_sessions(:r)"),
                           {"r": founder["refresh_plaintext"]})
    db_session.rollback()
