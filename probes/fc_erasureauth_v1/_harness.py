"""Shared helpers for the Account-Erasure Operation Authority examination (migrations 0085/0086).

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`. Subject: the DB
authority for account erasure — the bridge between a recent password reauthentication and the
phase-gated deletion of protected data:

  * account_erasure_reauth_receipts — governed; runtime SELECT only; a BEFORE-write guard and
    RLS WITH CHECK(false) forbid raw writes. Created only by the privileged reauth service.
  * account_erasure_operations — the operation state machine (status/phase); after 0086 the
    runtime loses INSERT/UPDATE/DELETE and may only drive it through SECURITY DEFINER
    functions account_erasure_begin_operation / _set_phase / _complete_operation.
  * personal_recall_erasure_current(owner) — the gate every erasure-scoped delete consults;
    true ONLY when the erasure GUCs match a LIVE operation row in the exact phase whose
    consumed reauth receipt is still bound to a current founder session.

The authority substrate (founder session + owner key + a Recall grant to delete) is built
through the product's own privileged services. RECALL STAYS INACTIVE (no ingestion/retrieval).
Read-only examiner; loopback only; no paid calls.
"""
from __future__ import annotations

import base64
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


def set_ctx(db, owner_id=None, *, access_jti=None, erasure=False, operation_id=None):
    if owner_id is not None:
        db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(owner_id)})
    if access_jti is not None:
        db.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": access_jti})
    if erasure:
        db.execute(text("SET LOCAL app.personal_recall_erasure_in_progress = 'true'"))
    if operation_id is not None:
        db.execute(text("SET LOCAL app.account_erasure_operation_id = :op"), {"op": str(operation_id)})


def founder_with_session(access_jti: str | None = None):
    """Ensure the canonical Founder + a live session (refresh token) + an owner key.

    Returns (FOUNDER_USER_ID, access_jti, RecallFounderAuthority)."""
    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.refresh_token import RefreshToken
    from app.models.user import User, UserRole
    from app.personal_recall.production_crypto import load_system_kek_from_env
    from app.personal_recall.production_ingestion import RecallFounderAuthority, get_or_create_owner_key
    from app.security import hash_password

    access_jti = access_jti or str(uuid.uuid4())
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
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex, access_jti=access_jti,
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
    return FOUNDER_USER_ID, access_jti, authority


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


def owner_key_id(owner_id):
    from app.db import SecurityKernelSessionLocal

    with SecurityKernelSessionLocal() as adb:
        set_ctx(adb, owner_id)
        return adb.execute(text(f"SELECT id FROM {OWNER_KEYS} WHERE owner_id=:o"),
                           {"o": str(owner_id)}).scalar_one()


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


def begin_operation(db, owner_id, receipt_id):
    return db.execute(text("SELECT account_erasure_begin_operation(:o,:r)"),
                      {"o": str(owner_id), "r": str(receipt_id)}).scalar_one()


def set_phase(db, operation_id, owner_id, phase):
    return db.execute(text("SELECT account_erasure_set_phase(:op,:o,:p)"),
                      {"op": str(operation_id), "o": str(owner_id), "p": phase}).scalar_one()


def complete_operation(db, operation_id, owner_id):
    return db.execute(text("SELECT account_erasure_complete_operation(:op,:o)"),
                      {"op": str(operation_id), "o": str(owner_id)}).scalar_one()


def grant_exists(superuser_db, grant_id) -> bool:
    return superuser_db.execute(text(f"SELECT count(*) FROM {GRANTS} WHERE id=:i"),
                                {"i": grant_id}).scalar_one() == 1
