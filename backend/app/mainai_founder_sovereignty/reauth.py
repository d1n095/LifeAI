"""Founder step-up re-authentication receipts.

Dedicated to step-up purposes. Does not widen ACCOUNT_ERASURE receipt purpose.
SELF-SET GUC != AUTHORITY. Password verification happens in Python; minting the
reauth row uses the migration/admin engine because mainai_app cannot INSERT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.db import migration_engine
from app.mainai_founder_sovereignty.types import SovereigntyError, StepUpPurpose
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.security import verify_password
from app.token_revocation import is_access_token_revoked

FOUNDER_STEP_UP_REAUTH_TTL_SECONDS = 300


@dataclass(frozen=True)
class FounderStepUpReauthReceipt:
    receipt_id: uuid.UUID
    owner_id: uuid.UUID
    access_jti: str
    purpose: str
    expires_at: datetime


def create_founder_step_up_reauth_receipt(
    db: Session,
    *,
    user: User,
    password: str,
    access_jti: str | None,
    purpose: StepUpPurpose,
    ttl_seconds: int = FOUNDER_STEP_UP_REAUTH_TTL_SECONDS,
) -> FounderStepUpReauthReceipt:
    if not access_jti:
        raise SovereigntyError("step_up_jti_required", "step-up requires the verified current session JTI")
    if not verify_password(password, user.password_hash):
        raise SovereigntyError("step_up_reauth_failed", "Founder step-up requires genuine re-authentication")
    if is_access_token_revoked(db, access_jti):
        raise SovereigntyError("step_up_session_revoked", "Founder step-up session is revoked")

    row = db.query(RefreshToken).filter_by(user_id=user.id, access_jti=access_jti).first()
    if row is None:
        raise SovereigntyError("step_up_session_stale", "Founder step-up session is not current for this owner")
    created_at = row.created_at.replace(tzinfo=timezone.utc) if row.created_at.tzinfo is None else row.created_at
    sessions_valid_after = user.sessions_valid_after.replace(tzinfo=timezone.utc)
    if created_at <= sessions_valid_after:
        raise SovereigntyError("step_up_session_stale", "Founder step-up session predates current session authority")

    receipt_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    with migration_engine.begin() as conn:
        conn.execute(
            sa_text(
                """
                INSERT INTO founder_step_up_reauth_receipts(
                    receipt_id, owner_id, access_jti, purpose, issued_at, expires_at
                ) VALUES (:receipt_id, :owner_id, :access_jti, :purpose, now(), :expires_at)
                """
            ),
            {
                "receipt_id": str(receipt_id),
                "owner_id": str(user.id),
                "access_jti": access_jti,
                "purpose": purpose.value,
                "expires_at": expires_at,
            },
        )
    return FounderStepUpReauthReceipt(
        receipt_id=receipt_id,
        owner_id=user.id,
        access_jti=access_jti,
        purpose=purpose.value,
        expires_at=expires_at,
    )
