"""Session-bound Founder identity.

PUBLIC FOUNDER UUID != AUTHORITY.
CALLER-SUPPLIED actor_id != AUTHENTICATION.
MODEL KNOWING FOUNDER ID != FOUNDER.
SERVICE ACCOUNT != FOUNDER.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.types import SovereigntyError
from app.models.user import User, UserRole

_FOUNDER_SENTINEL = FOUNDER_USER_ID


def session_user_id(db: Session) -> UUID:
    raw = db.execute(text("SELECT NULLIF(current_setting('app.current_user_id', true), '')")).scalar()
    if not raw:
        raise SovereigntyError(
            "unauthenticated",
            "Founder authority requires an authenticated session-bound identity",
        )
    return UUID(str(raw))


def reject_caller_supplied_mismatch(*, session_id: UUID, claimed_actor_id: UUID | None) -> None:
    if claimed_actor_id is None:
        return
    if claimed_actor_id != session_id:
        raise SovereigntyError(
            "caller_supplied_identity_rejected",
            "CALLER-SUPPLIED actor_id != AUTHENTICATION",
        )


def require_authenticated_founder(db: Session, *, claimed_actor_id: UUID | None = None) -> UUID:
    uid = session_user_id(db)
    reject_caller_supplied_mismatch(session_id=uid, claimed_actor_id=claimed_actor_id)
    if uid != _FOUNDER_SENTINEL:
        raise SovereigntyError(
            "not_founder",
            "FAMILY MEMBER != FOUNDER; ADMIN != FOUNDER; AI != FOUNDER; PUBLIC FOUNDER UUID != AUTHORITY",
        )
    user = db.get(User, uid)
    if user is None or user.role is not UserRole.founder or not user.is_active:
        raise SovereigntyError(
            "not_founder",
            "governed Founder binding requires the authenticated Founder row, not a public UUID",
        )
    return uid


def canonical_approval_snapshot_hash(
    *,
    principal_id: UUID,
    resource: str,
    action: str,
    scope: str,
    requested_data: dict | None,
    requested_limits: dict | None,
    requested_duration: str,
    risk_tier: str,
    consequences: str,
    session_id: str | None = None,
    device_id: str | None = None,
) -> str:
    payload = {
        "action": action,
        "consequences": consequences,
        "data": requested_data or {},
        "device": device_id or "",
        "duration": requested_duration,
        "limits": requested_limits or {},
        "principal": str(principal_id),
        "resource": resource,
        "risk": risk_tier,
        "scope": scope,
        "session": session_id or "",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
