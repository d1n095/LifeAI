"""Founder Identity / Session Revocation / Recovery — independent adversarial examination.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. Drives the REAL FastAPI auth routes via
the product TestClient AND the migration-0095 Founder identity/token guards +
reset_founder_password_with_token, so the examined boundary is the application+PostgreSQL
boundary. Does NOT duplicate the builder's identity-guard / forged-GUC session-revocation
coverage, nor the completed STOP-clear/publication/DB-authority suites.
reset_founder_password_with_token is tested nowhere in the product.

  FI1  reset_founder_password_with_token rejects forged / unknown / expired / consumed proof
  FI2  a valid reset bumps sessions_valid_after and revokes all Founder refresh tokens
  FI3  a reset proof is single-use (replay rejected)
  FI4  INTEGRATION: a previously-issued Founder access token is rejected by /me after a reset
  FI5  INTEGRATION: a previously-issued Founder refresh token is rejected by /refresh after a reset
  FI6  INTEGRATION: logout immediately invalidates the still-live access token (via /me)
  FI7  the founder-token guard also fences password-reset and email-verification proofs
  FI8  STOP activation does not block Founder authentication (by design, not a defect)
  FI9  fail-closed: missing cookie / tampered JWT / revoked-jti access token all 401
  FI10 INTEGRATION: replaying a rotated Founder refresh token revokes the whole family

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    ACCESS_COOKIE,
    CSRF_HEADER,
    FOUNDER_PASSWORD,
    REFRESH_COOKIE,
    call_reset,
    ensure_founder,
    founder_id,
    founder_session_rows,
    insert_password_reset_token,
    login,
    me,
    sha256_hex,
)


@pytest.fixture(autouse=True)
def _founder_exists():
    """Every test needs the canonical Founder present (tests that use the TestClient also get
    it from app startup; this makes the non-client tests self-sufficient and idempotent)."""
    ensure_founder()


def _new_hash():
    # Re-hash the SAME founder password: a reset's security effect (session invalidation) is
    # what we examine; keeping the password stable avoids cross-test contamination.
    from app.security import hash_password
    return hash_password(FOUNDER_PASSWORD)


# ---------------------------------------------------------------------------
# FI1 — reset_founder_password_with_token rejects forged / unknown / expired / consumed proof.
# ---------------------------------------------------------------------------
def test_FI1_reset_rejects_bad_proofs(db_session, superuser_db):
    # too-short / malformed proof
    with pytest.raises(DBAPIError, match="invalid Founder password-reset proof"):
        call_reset(db_session, "short", _new_hash())
    db_session.rollback()

    # well-formed but unknown token
    with pytest.raises(DBAPIError, match="invalid, expired, or consumed"):
        call_reset(db_session, "x" * 40, _new_hash())
    db_session.rollback()

    # expired token
    insert_password_reset_token(superuser_db, "e" * 40, expires_in=-60)
    with pytest.raises(DBAPIError, match="invalid, expired, or consumed"):
        call_reset(db_session, "e" * 40, _new_hash())
    db_session.rollback()

    # already-consumed token
    insert_password_reset_token(superuser_db, "c" * 40, used=True)
    with pytest.raises(DBAPIError, match="invalid, expired, or consumed"):
        call_reset(db_session, "c" * 40, _new_hash())
    db_session.rollback()


# ---------------------------------------------------------------------------
# FI2 — a valid reset bumps sessions_valid_after and revokes all Founder refresh tokens.
# ---------------------------------------------------------------------------
def test_FI2_valid_reset_invalidates_sessions(client, db_session, superuser_db):
    login(client)                                     # establishes a live Founder refresh token
    _, live_before = founder_session_rows(superuser_db)
    assert live_before >= 1

    plaintext = "v" * 40
    insert_password_reset_token(superuser_db, plaintext)
    returned = call_reset(db_session, plaintext, _new_hash())
    db_session.commit()
    assert returned == founder_id()

    _, live_after = founder_session_rows(superuser_db)
    assert live_after == 0                            # every Founder refresh token revoked
    assert superuser_db.execute(
        text("SELECT used_at IS NOT NULL FROM password_reset_tokens WHERE token_hash=:h"),
        {"h": sha256_hex(plaintext)}).scalar_one() is True


# ---------------------------------------------------------------------------
# FI3 — a reset proof is single-use (replay rejected).
# ---------------------------------------------------------------------------
def test_FI3_reset_proof_single_use(db_session, superuser_db):
    plaintext = "s" * 40
    insert_password_reset_token(superuser_db, plaintext)
    assert call_reset(db_session, plaintext, _new_hash()) == founder_id()
    db_session.commit()
    with pytest.raises(DBAPIError, match="invalid, expired, or consumed"):
        call_reset(db_session, plaintext, _new_hash())
    db_session.rollback()


# ---------------------------------------------------------------------------
# FI4 — INTEGRATION: a previously-issued Founder access token is rejected by /me after a reset.
# ---------------------------------------------------------------------------
def test_FI4_old_access_token_rejected_after_reset(client, db_session, superuser_db):
    login(client)
    assert me(client).status_code == 200             # the access cookie works pre-reset

    plaintext = "a" * 40
    insert_password_reset_token(superuser_db, plaintext)
    call_reset(db_session, plaintext, _new_hash())
    db_session.commit()

    # the SAME still-unexpired access cookie is now rejected by the real authenticated route
    assert me(client).status_code == 401


# ---------------------------------------------------------------------------
# FI5 — INTEGRATION: a previously-issued Founder refresh token is rejected by /refresh after reset.
# ---------------------------------------------------------------------------
def test_FI5_old_refresh_token_rejected_after_reset(client, db_session, superuser_db):
    csrf = login(client)

    plaintext = "r" * 40
    insert_password_reset_token(superuser_db, plaintext)
    call_reset(db_session, plaintext, _new_hash())
    db_session.commit()

    r = client.post("/api/auth/refresh", headers={CSRF_HEADER: csrf})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# FI6 — INTEGRATION: logout immediately invalidates the still-live access token.
# ---------------------------------------------------------------------------
def test_FI6_logout_invalidates_access_immediately(client):
    csrf = login(client)
    assert me(client).status_code == 200
    assert client.post("/api/auth/logout", headers={CSRF_HEADER: csrf}).status_code == 200
    # access cookie is cleared by logout; even if replayed it is revoked server-side
    assert me(client).status_code == 401


# ---------------------------------------------------------------------------
# FI7 — the founder-token guard also fences password-reset and email-verification proofs.
# ---------------------------------------------------------------------------
def test_FI7_founder_token_guard_covers_reset_and_verification(db_session):
    fid = founder_id()
    db_session.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(fid)})
    with pytest.raises(DBAPIError, match="runtime role cannot mint, alter, or delete Founder authentication proof"):
        db_session.execute(text(
            "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at) "
            "VALUES (gen_random_uuid(), :u, :th, now(), now()+interval '10 min')"),
            {"u": fid, "th": sha256_hex("runtime-forged-" + uuid.uuid4().hex)})
    db_session.rollback()

    db_session.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(fid)})
    with pytest.raises(DBAPIError, match="runtime role cannot mint, alter, or delete Founder authentication proof"):
        db_session.execute(text(
            "INSERT INTO email_verification_tokens (id, user_id, token_hash, created_at, expires_at) "
            "VALUES (gen_random_uuid(), :u, :th, now(), now()+interval '10 min')"),
            {"u": fid, "th": sha256_hex("runtime-forged-" + uuid.uuid4().hex)})
    db_session.rollback()


# ---------------------------------------------------------------------------
# FI8 — STOP activation does not block Founder authentication (by design: the Founder must stay
#        able to authenticate in order to clear a STOP). Recorded as expected behaviour, not a defect.
# ---------------------------------------------------------------------------
def test_FI8_stop_does_not_block_founder_authentication(client, superuser_db):
    from app.workforce.kill_switch import activate_kill_switch

    # Activate the Founder owner's STOP through the privileged boundary (STOP activation is a
    # privileged operation; the runtime cannot FOR UPDATE the authority-epoch table itself).
    superuser_db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(founder_id())})
    activate_kill_switch(superuser_db, owner_id=founder_id(), reason="examiner STOP during auth")
    superuser_db.commit()

    csrf = login(client)                              # login still succeeds under an active STOP
    assert me(client).status_code == 200
    assert client.post("/api/auth/refresh", headers={CSRF_HEADER: csrf}).status_code == 200


# ---------------------------------------------------------------------------
# FI9 — fail-closed: missing cookie, tampered JWT, and a revoked-jti access token all 401.
# ---------------------------------------------------------------------------
def test_FI9_auth_fails_closed(client):
    # no cookie at all
    client.cookies.clear()
    assert me(client).status_code == 401

    # a syntactically-broken / tampered access token
    assert client.get("/api/auth/me", cookies={ACCESS_COOKIE: "not.a.valid.jwt"}).status_code == 401

    # a live session, then the jti explicitly revoked -> the still-unexpired token is refused
    login(client)
    assert me(client).status_code == 200
    # revoke via logout (server-side jti revocation), then replay the (cleared) access value
    import jwt as _jwt
    access = client.cookies.get(ACCESS_COOKIE)
    csrf = me(client).json().get("csrf_token", "")
    client.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
    assert client.get("/api/auth/me", cookies={ACCESS_COOKIE: access}).status_code == 401


# ---------------------------------------------------------------------------
# FI10 — INTEGRATION: replaying a rotated Founder refresh token revokes the whole family.
# ---------------------------------------------------------------------------
def test_FI10_refresh_replay_revokes_family(client):
    csrf_old = login(client)
    old_refresh = client.cookies.get(REFRESH_COOKIE)

    # rotate once: the old token is now spent, a new one takes its place
    r1 = client.post("/api/auth/refresh", headers={CSRF_HEADER: csrf_old})
    assert r1.status_code == 200, r1.text
    csrf_new = r1.json().get("csrf_token", "")

    # replay the OLD (now revoked) refresh token -> reuse detected -> 401, family revoked
    replay = client.post("/api/auth/refresh",
                         headers={CSRF_HEADER: csrf_old},
                         cookies={REFRESH_COOKIE: old_refresh})
    assert replay.status_code == 401

    # the legitimate NEW token is now dead too (whole family was revoked on reuse)
    assert client.post("/api/auth/refresh", headers={CSRF_HEADER: csrf_new}).status_code == 401
