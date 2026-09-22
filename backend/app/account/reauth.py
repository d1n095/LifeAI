from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.db import migration_engine
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.security import verify_password
from app.token_revocation import is_access_token_revoked

ACCOUNT_ERASURE_REAUTH_TTL_SECONDS = 300


class AccountErasureReauthError(PermissionError):
    pass


@dataclass(frozen=True)
class AccountErasureReauthReceipt:
    receipt_id: uuid.UUID
    owner_id: uuid.UUID
    access_jti: str
    expires_at: datetime


def create_account_erasure_reauth_receipt(
    db: Session,
    *,
    user: User,
    password: str,
    access_jti: str | None,
    ttl_seconds: int = ACCOUNT_ERASURE_REAUTH_TTL_SECONDS,
) -> AccountErasureReauthReceipt:
    """Create a short-lived account-erasure receipt after real password verification.

    The receipt is the bridge between the router's password check and the DB-level erasure
    boundary. Ordinary runtime SQL cannot insert receipt rows; this function uses the
    migration/admin engine only after verifying the presented password against the current
    user's password hash and checking that the access JTI still belongs to a current session.
    """
    if not access_jti:
        raise AccountErasureReauthError("account erasure requires an authenticated access session")
    if not verify_password(password, user.password_hash):
        raise AccountErasureReauthError("wrong password")
    if is_access_token_revoked(db, access_jti):
        raise AccountErasureReauthError("account erasure session is revoked")

    row = db.query(RefreshToken).filter_by(user_id=user.id, access_jti=access_jti).first()
    if row is None:
        raise AccountErasureReauthError("account erasure session is not current for this owner")
    created_at = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    sessions_valid_after = user.sessions_valid_after.replace(tzinfo=timezone.utc)
    if created_at <= sessions_valid_after:
        raise AccountErasureReauthError("account erasure session predates current session authority")

    receipt_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with migration_engine.begin() as conn:
        conn.execute(
            sa_text(
                """
                INSERT INTO account_erasure_reauth_receipts(
                    receipt_id, owner_id, access_jti, purpose, issued_at, expires_at
                ) VALUES (:receipt_id, :owner_id, :access_jti, 'ACCOUNT_ERASURE', now(), :expires_at)
                """
            ),
            {
                "receipt_id": str(receipt_id),
                "owner_id": str(user.id),
                "access_jti": access_jti,
                "expires_at": expires_at,
            },
        )
    return AccountErasureReauthReceipt(receipt_id=receipt_id, owner_id=user.id, access_jti=access_jti, expires_at=expires_at)
