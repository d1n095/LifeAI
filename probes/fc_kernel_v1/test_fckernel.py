"""Codex Security Kernel candidates — independent examination (examiner-owned, read-only).

Subjects (archive-delivered, bundle-verified):
  STOP-clear  20d5a14301f574efe0ba3cc1d2d2f694b6cc1ef4  (codex/founder-stop-clear-reauthorization)
  FA-PRIV     078312d7abce3f5c0f87c9e7b30161973e55d532  (codex/fa-priv-closure-integration-readiness)
This suite runs against the FA-PRIV tip 078312d7 (which contains STOP-clear as its parent).

Drives the NEW kernel surfaces directly:
  app.founder_authority.stop_clear (authenticated one-shot STOP-clear = CO-10),
  app.workforce.stop_authority.read_stop_authority_snapshot (authority UNKNOWN fail-closed),
  DB privilege REVOKEs on workforce_authority_epoch / founder_stop_clear_* (FA-PRIV).

The authenticated happy-path clear (password + live access session + one-shot consume + replay
rejection) is additionally verified by code reading in the verdict; no founder-session helper exists
in the harness to execute it here. Codex's own EVIDENCE/XML are read as CLAIMS only — certification
rests on these independent checks. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_PW = "CorrectHorseBattery9!"


# ---------------------------------------------------------------------------
# FA-PRIV — the ordinary mainai_app runtime cannot write STOP/authority state
# ---------------------------------------------------------------------------

def test_K1_runtime_cannot_write_authority_epoch(db_session):
    """Ordinary runtime (session_user=mainai_app) is DB-denied from mutating the STOP epoch —
    it cannot grant itself authority or clear STOP by writing the canonical table."""
    from sqlalchemy.exc import ProgrammingError, InternalError
    with pytest.raises((ProgrammingError, InternalError)) as ei:
        db_session.execute(sa_text("UPDATE workforce_authority_epoch SET stopped=false WHERE scope_key='GLOBAL'"))
        db_session.flush()
    assert "permission denied" in str(ei.value).lower() or "denied" in str(ei.value).lower()
    db_session.rollback()


def test_K2_runtime_direct_clear_owner_stop_is_denied(db_session, owner_id):
    """The OLD string-ack direct clear path is dead for the runtime: clear_owner_stop as mainai_app
    is refused at the database (it UPDATEs the revoked epoch table)."""
    from app.workforce.kill_switch import clear_owner_stop
    with pytest.raises(Exception) as ei:
        clear_owner_stop(db_session, owner_id=owner_id, founder_ack="founder_ack:resume")
        db_session.flush()
    msg = str(ei.value).lower()
    # either DB-denied, or the legacy string-ack path is explicitly disabled in code
    assert ("denied" in msg or "permission" in msg
            or "legacy acknowledgement" in msg or "authenticated founder stop-clear" in msg), ei.value
    db_session.rollback()


def test_K3_runtime_cannot_mint_founder_identity(db_session):
    """FA-PRIV adjacent (migration 0095): the runtime role cannot INSERT a founder-role user, so it
    cannot self-escalate to the identity that authorizes STOP-clear."""
    from app.models.user import User
    from app.security import hash_password, utcnow_seconds_baseline
    from sqlalchemy.exc import ProgrammingError, InternalError, IntegrityError
    now = utcnow_seconds_baseline()
    u = User(email=f"esc-{uuid.uuid4().hex[:8]}@x.com", password_hash=hash_password("x"), role="founder",
             email_verified=True, email_verified_at=now, sessions_valid_after=now)
    db_session.add(u)
    with pytest.raises((ProgrammingError, InternalError, IntegrityError)):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# FA-PRIV / PI-STOP C2 — authority state UNKNOWN is a fail-closed exception
# ---------------------------------------------------------------------------

def test_K4_authority_unknown_fails_closed(db_session, owner_id):
    """read_stop_authority_snapshot raises (never returns an implicit 'allowed') when the canonical
    epoch rows are absent — closing the authority-UNKNOWN gap flagged in PI-STOP."""
    from app.workforce.stop_authority import read_stop_authority_snapshot, StopAuthorityBlocked
    fresh_owner = uuid.uuid4()  # no epoch rows exist for this owner
    with pytest.raises(StopAuthorityBlocked) as ei:
        read_stop_authority_snapshot(db_session, owner_id=fresh_owner)
    assert "UNKNOWN" in str(ei.value.code if hasattr(ei.value, "code") else ei.value) or \
        "unknown" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# CO-10 — STOP-clear requires an authenticated current Founder session (not a string)
# ---------------------------------------------------------------------------

def test_K5_issue_requires_founder_identity(db_session, make_verified_user):
    """A non-Founder cannot issue a STOP-clear authorization."""
    from app.founder_authority.stop_clear import issue_stop_clear_authorization
    from app.founder_authority.service import FounderAuthorityError
    member, pw = make_verified_user(role="member")
    with pytest.raises(FounderAuthorityError):
        issue_stop_clear_authorization(db_session, user=member, password=pw, access_jti="x",
                                       scope="owner", target_owner_id=member.id, expected_epoch=0,
                                       operation_id=uuid.uuid4())


def _pinned_founder(superuser_db):
    """Construct THE pinned Founder (user.id == FOUNDER_USER_ID) via the schema-owner session so we
    can reach the authenticated-clear inner legs. The runtime role cannot mint this (K3)."""
    from app.founder import FOUNDER_USER_ID
    from app.models.user import User
    from app.security import hash_password, utcnow_seconds_baseline
    now = utcnow_seconds_baseline()
    existing = superuser_db.get(User, FOUNDER_USER_ID)
    if existing is not None:
        return existing, "CorrectHorseBattery9!"
    u = User(id=FOUNDER_USER_ID, email="founder@lifeos.local", password_hash=hash_password(FOUNDER_PW),
             role="founder", email_verified=True, email_verified_at=now, sessions_valid_after=now)
    superuser_db.add(u); superuser_db.commit()
    return u, FOUNDER_PW


def test_K6_issue_requires_password_and_live_session(db_session, superuser_db):
    """Even the pinned Founder cannot issue a STOP-clear without a live access session and the
    correct password — CO-10 is an authenticated proof, no longer a bare string."""
    from app.founder_authority.stop_clear import issue_stop_clear_authorization, StopClearError
    founder, pw = _pinned_founder(superuser_db)
    with pytest.raises(StopClearError):   # access_jti=None -> password+session required
        issue_stop_clear_authorization(db_session, user=founder, password=pw, access_jti=None,
                                       scope="owner", target_owner_id=founder.id, expected_epoch=0,
                                       operation_id=uuid.uuid4())
    with pytest.raises(StopClearError):   # wrong password
        issue_stop_clear_authorization(db_session, user=founder, password="wrong", access_jti="jti",
                                       scope="owner", target_owner_id=founder.id, expected_epoch=0,
                                       operation_id=uuid.uuid4())


def test_K7_clear_with_fabricated_authorization_is_rejected(superuser_db):
    """App-level rejection of a fabricated authorization id (run on a privileged connection so the
    clear LOGIC is reached rather than the FA-PRIV DB denial that K1/K2 already prove for runtime)."""
    from app.founder_authority.stop_clear import clear_stop_with_authorization, StopClearError
    founder, pw = _pinned_founder(superuser_db)
    with pytest.raises(StopClearError):
        clear_stop_with_authorization(superuser_db, user=founder, authorization_id=uuid.uuid4(),
                                      operation_id=uuid.uuid4(), scope="owner", target_owner_id=founder.id,
                                      expected_epoch=0, access_jti="jti")
