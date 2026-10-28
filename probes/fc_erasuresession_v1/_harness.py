"""Shared helpers for Priority 8 — Revoked Founder Session vs Account-Erasure Authority.

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`.

This suite re-opens the LOW/informational observation recorded by `fc_crossauth_v1` and
subjects it to executable verification, because the earlier dismissal ("the window closes the
next second") was an unproven claim about wall-clock behaviour. The decisive mechanism:

  account_erasure_receipt_session_current(owner, access_jti)   (migration 0086, authoritative;
  never redefined later) decides whether an erasure reauth receipt is still bound to a current
  session with:

      ...
      AND NOT EXISTS (SELECT 1 FROM revoked_access_tokens rat WHERE rat.jti = access_jti)
      AND rt.created_at > u.sessions_valid_after            -- (A) stored vs stored

  (A) is a comparison between two PERSISTENT stored timestamps. It is NOT a function of the
  wall clock, so it does not "close the next second"; it stays whatever it was until a row
  changes. The helper NEVER inspects refresh_tokens.revoked_at.

  Every real session-revocation primitive in the product sets sessions_valid_after to the
  CURRENT SECOND, truncated down, to match the integer-second JWT `iat` precision:
    * reset_founder_password_with_token (0095): sessions_valid_after = date_trunc('second', clock_timestamp())
    * revoke_current_founder_sessions   (0095): same
    * revoke_all_sessions_for_user      (app/token_revocation.py): utcnow_seconds()
  and each also sets refresh_tokens.revoked_at on the very session it is revoking.

  Consequence: if the revoked session's refresh row was created at any sub-second offset
  INSIDE the second the revocation truncates to (created_at = S.xxx, sessions_valid_after =
  S.000), then created_at > sessions_valid_after stays TRUE after the revoke, and because the
  helper ignores revoked_at, the just-revoked session still satisfies erasure authority — for
  as long as the rows stand (a fresh begin_operation is still bounded by the receipt's <=5 min
  lifetime, but an already-STARTED operation re-checks only session-currency + consumed_at, so
  it continues UNBOUNDED under the revoked session).

Contrast with the certified fail-closed test (fc_erasureauth AE9): AE9 revokes with
`sessions_valid_after = now() + interval '1 minute'` — a full minute AHEAD of the token — and
concludes fail-closed. No real revocation path revokes that aggressively; they all truncate to
the current second. This suite drives the REAL reset function and the REAL
revoke_all_sessions_for_user and shows the gate does not re-close in the same-second case.

RECALL STAYS INACTIVE (no ingestion/retrieval): the protected-data row deleted in ES3 is a
Recall GRANT minted through the product's own privileged service purely as the erasure
target; nothing is ingested or retrieved. Read-only examiner; loopback only; no paid calls.
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
GRANTS = "personal_recall_grants"
OWNER_KEYS = "personal_recall_owner_keys"
OPERATIONS = "account_erasure_operations"
RECEIPTS = "account_erasure_reauth_receipts"


def sha256_hex(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def set_ctx(db, owner_id=None, *, access_jti=None, erasure=False, operation_id=None):
    if owner_id is not None:
        db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(owner_id)})
    if access_jti is not None:
        db.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": access_jti})
    if erasure:
        db.execute(text("SET LOCAL app.personal_recall_erasure_in_progress = 'true'"))
    if operation_id is not None:
        db.execute(text("SET LOCAL app.account_erasure_operation_id = :op"), {"op": str(operation_id)})


def founder_with_session(access_jti: str | None = None, *, refresh_plaintext: str | None = None):
    """Ensure the canonical Founder + a live session (refresh token) + an owner key.

    The refresh row's token_hash is sha256(refresh_plaintext) so the REAL
    revoke_current_founder_sessions / revoke_all_sessions_for_user path (which requires the
    plaintext refresh proof) can be exercised against it. created_at defaults to 2s ago.

    Returns dict(owner, jti, refresh_plaintext, authority)."""
    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.refresh_token import RefreshToken
    from app.models.user import User, UserRole
    from app.personal_recall.production_crypto import load_system_kek_from_env
    from app.personal_recall.production_ingestion import RecallFounderAuthority, get_or_create_owner_key
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
        set_ctx(adb, FOUNDER_USER_ID, access_jti=access_jti)
        get_or_create_owner_key(
            adb, owner_id=FOUNDER_USER_ID, system_kek=load_system_kek_from_env(),
            authority=RecallFounderAuthority(owner_id=FOUNDER_USER_ID, access_jti=access_jti,
                                             actor="founder_session"))
        adb.commit()
        from app.personal_recall.production_ingestion import recall_founder_authority_from_user
        authority = recall_founder_authority_from_user(user=adb.get(User, FOUNDER_USER_ID),
                                                       access_jti=access_jti)
    return {"owner": FOUNDER_USER_ID, "jti": access_jti,
            "refresh_plaintext": refresh_plaintext, "authority": authority}


def mint_recall_grant(owner_id, authority, *, session_id=None):
    from app.db import SecurityKernelSessionLocal
    from app.personal_recall.production_ingestion import create_recall_grant

    with SecurityKernelSessionLocal() as adb:
        set_ctx(adb, owner_id, access_jti=authority.access_jti)
        row = create_recall_grant(adb, owner_id=owner_id, session_id=session_id or f"erase-exam-{uuid.uuid4()}",
                                  purpose="founder_file_ingestion", resource_classes=("file",),
                                  disclosure_level="snippet", can_disclose=True, authority=authority)
        adb.commit()
        return row.id


def make_reauth_receipt(owner_id, access_jti, *, ttl_seconds=240):
    """Create a legitimate account-erasure reauth receipt (privileged service)."""
    from app.account.reauth import create_account_erasure_reauth_receipt
    from app.db import SecurityKernelSessionLocal
    from app.models.user import User

    with SecurityKernelSessionLocal() as adb:
        set_ctx(adb, owner_id, access_jti=access_jti)
        user = adb.get(User, owner_id)
        receipt = create_account_erasure_reauth_receipt(
            adb, user=user, password=FOUNDER_PASSWORD, access_jti=access_jti, ttl_seconds=ttl_seconds)
        adb.commit()
    return receipt.receipt_id


# --- the REAL Founder revocation primitives -------------------------------------------------

def insert_password_reset_token(superuser_db, plaintext, *, expires_in=600):
    """Insert a Founder password-reset token (schema-owner setup write)."""
    from app.founder import FOUNDER_USER_ID
    superuser_db.execute(text(
        "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at, used_at) "
        "VALUES (:id, :u, :th, now(), :exp, NULL)"),
        {"id": uuid.uuid4(), "u": FOUNDER_USER_ID, "th": sha256_hex(plaintext),
         "exp": datetime.utcnow() + timedelta(seconds=expires_in)})
    superuser_db.commit()


def real_founder_password_reset(db, plaintext, *, new_password=None):
    """Drive the REAL reset_founder_password_with_token(token, hash) production function.
    Returns the founder uuid it returns."""
    from app.security import hash_password
    new_hash = hash_password(new_password or FOUNDER_PASSWORD)
    rid = db.execute(text("SELECT reset_founder_password_with_token(:t, :h)"),
                     {"t": plaintext, "h": new_hash}).scalar_one()
    db.commit()
    return rid


def real_revoke_all_sessions(db, refresh_plaintext):
    """Drive the REAL revoke_all_sessions_for_user production service for the Founder (which in
    turn calls the SECURITY DEFINER revoke_current_founder_sessions with the plaintext proof)."""
    from app.founder import FOUNDER_USER_ID
    from app.token_revocation import revoke_all_sessions_for_user
    revoke_all_sessions_for_user(db, FOUNDER_USER_ID, founder_refresh_token=refresh_plaintext)


# --- stored-state inspection / controlled timestamp placement -------------------------------

def session_row(superuser_db, access_jti):
    """(sessions_valid_after, rt.created_at, rt.revoked_at) for the founder refresh row."""
    from app.founder import FOUNDER_USER_ID
    return superuser_db.execute(text(
        "SELECT u.sessions_valid_after, rt.created_at, rt.revoked_at "
        "FROM users u JOIN refresh_tokens rt ON rt.user_id=u.id AND rt.access_jti=:j "
        "WHERE u.id=:i"), {"j": access_jti, "i": FOUNDER_USER_ID}).one()


def place_created_at(superuser_db, access_jti, created_at):
    """Set the refresh row's created_at to a fixed value — models the real-world coincidence
    that the session's login landed at that instant. Deterministic (no wall-clock race)."""
    superuser_db.execute(
        text("UPDATE refresh_tokens SET created_at=:c WHERE access_jti=:j"),
        {"c": created_at, "j": access_jti})
    superuser_db.commit()


def set_session_timestamps(superuser_db, access_jti, *, sessions_valid_after, created_at, revoked_at):
    """Pin all three stored timestamps to fixed values (used to prove wall-clock independence)."""
    from app.founder import FOUNDER_USER_ID
    superuser_db.execute(text("UPDATE users SET sessions_valid_after=:s WHERE id=:i"),
                         {"s": sessions_valid_after, "i": FOUNDER_USER_ID})
    superuser_db.execute(
        text("UPDATE refresh_tokens SET created_at=:c, revoked_at=:r WHERE access_jti=:j"),
        {"c": created_at, "r": revoked_at, "j": access_jti})
    superuser_db.commit()


def session_current(db, owner_id, access_jti):
    """Call the real helper under the current GUC context."""
    return db.execute(text("SELECT account_erasure_receipt_session_current(:o,:j)"),
                      {"o": str(owner_id), "j": access_jti}).scalar_one()


def begin_operation(db, owner_id, receipt_id):
    return db.execute(text("SELECT account_erasure_begin_operation(:o,:r)"),
                      {"o": str(owner_id), "r": str(receipt_id)}).scalar_one()


def set_phase(db, operation_id, owner_id, phase):
    return db.execute(text("SELECT account_erasure_set_phase(:op,:o,:p)"),
                      {"op": str(operation_id), "o": str(owner_id), "p": phase}).scalar_one()


def grant_exists(superuser_db, grant_id) -> bool:
    return superuser_db.execute(text(f"SELECT count(*) FROM {GRANTS} WHERE id=:i"),
                                {"i": grant_id}).scalar_one() == 1
