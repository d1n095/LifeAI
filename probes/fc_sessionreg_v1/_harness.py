"""Shared helpers for Priority 9 — Independent Regression Gate.

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`.

This gate encodes the REQUIRED invariant exposed by Priority 8 (`fc_erasuresession_v1`):

  Every session-gated authority helper must treat a session that any real revocation primitive
  has invalidated as NOT current — regardless of sub-second truncation — by honouring
  refresh_tokens.revoked_at / expires_at and using timestamp semantics consistent with the
  access-token `iat` check.

The codebase already contains the correct pattern: `personal_recall_founder_authority_current`
(migration 0083) checks `rt.revoked_at IS NULL AND rt.expires_at > now() AND rt.created_at >
u.sessions_valid_after`, and `revoke_current_founder_sessions` (0095) checks revoked_at+expiry
too. Only `account_erasure_receipt_session_current` (0086) omits them. The gate asserts the
invariant on all three session-gated surfaces; the erasure assertions are marked xfail(strict)
keyed to the confirmed defect, so they report expected-fail on the broken build and will turn
into a loud XPASS->failure the moment the one-line fix lands (the signal to enforce the gate).

Read-only examiner. RECALL INACTIVE. Loopback only; no paid calls. first_cycle_go=false.
"""
from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("PERSONAL_RECALL_SYSTEM_KEK_B64", base64.b64encode(b"r" * 32).decode("ascii"))
os.environ.setdefault("PERSONAL_RECALL_SYSTEM_KEK_VERSION", "test-v1")

from sqlalchemy import text  # noqa: E402

FOUNDER_PASSWORD = "CorrectHorseBattery9!"

# The confirmed-defect key shared by every xfail in this gate.
P8_DEFECT = ("CONFIRMED DEFECT (Priority 8): account_erasure_receipt_session_current "
             "(0086) ignores refresh_tokens.revoked_at/expires_at and compares a microsecond "
             "created_at against a second-truncated sessions_valid_after with strict '>', so a "
             "just-revoked same-second session is still treated as current. Fix: add "
             "`AND rt.revoked_at IS NULL` (and align the freshness comparison). When fixed this "
             "xfail becomes an XPASS -> enforce the gate.")


def sha256_hex(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def set_ctx(db, owner_id=None, *, access_jti=None, recall_founder_jti=None):
    if owner_id is not None:
        db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(owner_id)})
    if access_jti is not None:
        db.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": access_jti})
    if recall_founder_jti is not None:
        db.execute(text("SET LOCAL app.personal_recall_founder_access_jti = :j"),
                   {"j": recall_founder_jti})


def founder_with_session(access_jti=None, *, refresh_plaintext=None):
    """Canonical Founder + a live refresh session (token_hash = sha256(refresh_plaintext) so the
    real revoke_current_founder_sessions path can match it). Returns dict(owner, jti, refresh_plaintext)."""
    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.refresh_token import RefreshToken
    from app.models.user import User, UserRole
    from app.security import hash_password

    access_jti = access_jti or str(uuid.uuid4())
    refresh_plaintext = refresh_plaintext or (uuid.uuid4().hex + uuid.uuid4().hex)
    now = datetime.utcnow()
    with SecurityKernelSessionLocal() as adb:
        user = adb.get(User, FOUNDER_USER_ID)
        if user is None:
            user = User(id=FOUNDER_USER_ID, email="founder@lifeos.local",
                        password_hash=hash_password(FOUNDER_PASSWORD), role=UserRole.founder,
                        is_active=True, email_verified=True, email_verified_at=now,
                        sessions_valid_after=now - timedelta(seconds=2))
            adb.add(user)
        else:
            user.role = UserRole.founder
            user.is_active = True
            user.sessions_valid_after = now - timedelta(seconds=2)
        adb.flush()
        adb.add(RefreshToken(
            user_id=FOUNDER_USER_ID, family_id=uuid.uuid4(),
            token_hash=sha256_hex(refresh_plaintext), access_jti=access_jti,
            csrf_token=uuid.uuid4().hex + uuid.uuid4().hex, created_at=now,
            expires_at=now + timedelta(hours=1)))
        adb.commit()
    return {"owner": FOUNDER_USER_ID, "jti": access_jti, "refresh_plaintext": refresh_plaintext}


def insert_password_reset_token(superuser_db, plaintext, *, expires_in=600):
    from app.founder import FOUNDER_USER_ID
    superuser_db.execute(text(
        "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at, used_at) "
        "VALUES (:id, :u, :th, now(), :exp, NULL)"),
        {"id": uuid.uuid4(), "u": FOUNDER_USER_ID, "th": sha256_hex(plaintext),
         "exp": datetime.utcnow() + timedelta(seconds=expires_in)})
    superuser_db.commit()


def real_founder_password_reset(db, plaintext):
    from app.security import hash_password
    rid = db.execute(text("SELECT reset_founder_password_with_token(:t, :h)"),
                     {"t": plaintext, "h": hash_password(FOUNDER_PASSWORD)}).scalar_one()
    db.commit()
    return rid


def session_row(superuser_db, access_jti):
    from app.founder import FOUNDER_USER_ID
    return superuser_db.execute(text(
        "SELECT u.sessions_valid_after, rt.created_at, rt.revoked_at, rt.expires_at "
        "FROM users u JOIN refresh_tokens rt ON rt.user_id=u.id AND rt.access_jti=:j "
        "WHERE u.id=:i"), {"j": access_jti, "i": FOUNDER_USER_ID}).one()


def place_created_at(superuser_db, access_jti, created_at):
    superuser_db.execute(text("UPDATE refresh_tokens SET created_at=:c WHERE access_jti=:j"),
                         {"c": created_at, "j": access_jti})
    superuser_db.commit()


def set_token_fields(superuser_db, access_jti, **fields):
    assigns = ", ".join(f"{k}=:{k}" for k in fields)
    params = dict(fields); params["j"] = access_jti
    superuser_db.execute(text(f"UPDATE refresh_tokens SET {assigns} WHERE access_jti=:j"), params)
    superuser_db.commit()


def erasure_session_current(db, owner_id, access_jti):
    """account_erasure_receipt_session_current — needs app.current_user_id + app.current_access_jti."""
    return db.execute(text("SELECT account_erasure_receipt_session_current(:o,:j)"),
                      {"o": str(owner_id), "j": access_jti}).scalar_one()


def recall_founder_authority_current(db, owner_id):
    """personal_recall_founder_authority_current — needs app.current_user_id +
    app.personal_recall_founder_access_jti. (The correct pattern the erasure helper must match.)"""
    return db.execute(text("SELECT personal_recall_founder_authority_current(:o)"),
                      {"o": str(owner_id)}).scalar_one()
