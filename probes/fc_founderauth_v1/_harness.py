"""Shared helpers for the Founder Identity / Session Revocation / Recovery examination.

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`. Subject: the
security-critical Founder identity and authentication boundary (migration 0095 + the real
FastAPI auth routes):

  * security_kernel_founder_token_guard — runtime cannot mint/alter/delete any Founder auth
    proof (refresh / password-reset / email-verification tokens).
  * reset_founder_password_with_token(token, hash) — the only runtime-reachable Founder
    password-reset path; single-use, expiry/consumed checks; bumps sessions_valid_after and
    revokes all Founder refresh tokens.
  * get_current_user — rejects an access token whose jti is revoked OR whose iat <=
    users.sessions_valid_after (the app side of the DB revocation signal).

This suite drives the REAL auth routes (login/refresh/logout/me) via the product's TestClient
AND the DB functions/guards, so the examined boundary is the application+PostgreSQL boundary,
not isolated helpers. It does NOT duplicate the builder's identity-guard / forged-GUC
session-revocation coverage. Read-only examiner; loopback only; no paid calls.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
CSRF_HEADER = "x-csrf-token"
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


def founder_id():
    from app.founder import FOUNDER_USER_ID
    return FOUNDER_USER_ID


def ensure_founder():
    """Idempotently ensure the canonical Founder exists with the test password, through the
    privileged (schema-owner) boundary (the runtime role is forbidden from minting it)."""
    from datetime import timedelta

    from app.db import SecurityKernelSessionLocal
    from app.models.user import User, UserRole
    from app.security import hash_password

    now = datetime.utcnow()
    with SecurityKernelSessionLocal() as adb:
        u = adb.get(User, founder_id())
        if u is None:
            adb.add(User(id=founder_id(), email=FOUNDER_EMAIL,
                         password_hash=hash_password(FOUNDER_PASSWORD), role=UserRole.founder,
                         is_active=True, email_verified=True, email_verified_at=now,
                         sessions_valid_after=now - timedelta(seconds=2)))
        else:
            u.role = UserRole.founder
            u.is_active = True
            u.email_verified = True
        adb.commit()
    return founder_id()


def sha256_hex(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def login(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json().get("csrf_token", "")


def me(client):
    return client.get("/api/auth/me")


def insert_password_reset_token(superuser_db, plaintext, *, user_id=None, expires_in=600, used=False):
    """Insert a Founder password-reset token via the privileged (schema-owner) session so the
    founder-token guard (which only fences mainai_app) permits the setup write."""
    uid = user_id or founder_id()
    superuser_db.execute(text(
        "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at, used_at) "
        "VALUES (:id, :u, :th, now(), :exp, :used)"),
        {"id": uuid.uuid4(), "u": uid, "th": sha256_hex(plaintext),
         "exp": datetime.utcnow() + timedelta(seconds=expires_in),
         "used": datetime.utcnow() if used else None})
    superuser_db.commit()


def call_reset(db, plaintext, new_hash):
    return db.execute(text("SELECT reset_founder_password_with_token(:t, :h)"),
                      {"t": plaintext, "h": new_hash}).scalar_one()


def founder_session_rows(superuser_db):
    """(sessions_valid_after, count of live founder refresh tokens)."""
    sva = superuser_db.execute(
        text("SELECT sessions_valid_after FROM users WHERE id=:i"), {"i": founder_id()}).scalar_one()
    live = superuser_db.execute(
        text("SELECT count(*) FROM refresh_tokens WHERE user_id=:i AND revoked_at IS NULL"),
        {"i": founder_id()}).scalar_one()
    return sva, live
