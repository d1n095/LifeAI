"""Governed refresh-token minting. Raw INSERT as `mainai_app` is denied."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.refresh_token import RefreshToken


def mint_refresh_token(
    db: Session,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    token_hash: str,
    csrf_token: str,
    expires_at: datetime,
    access_jti: str | None = None,
    created_at: datetime | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
    replaces_id: uuid.UUID | None = None,
    token_id: uuid.UUID | None = None,
) -> RefreshToken:
    """Insert a refresh-token row through the SECURITY DEFINER mint function."""
    row_id = token_id or uuid.uuid4()
    db.execute(
        text(
            "SELECT mint_refresh_token("
            "CAST(:id AS uuid), CAST(:user_id AS uuid), CAST(:family_id AS uuid), "
            ":token_hash, :access_jti, :csrf_token, "
            "CAST(:created_at AS timestamp), CAST(:expires_at AS timestamp), "
            ":user_agent, :ip_address, CAST(:replaces_id AS uuid))"
        ),
        {
            "id": str(row_id),
            "user_id": str(user_id),
            "family_id": str(family_id),
            "token_hash": token_hash,
            "access_jti": access_jti,
            "csrf_token": csrf_token,
            "created_at": created_at,
            "expires_at": expires_at,
            "user_agent": user_agent,
            "ip_address": ip_address,
            "replaces_id": str(replaces_id) if replaces_id is not None else None,
        },
    )
    db.flush()
    row = db.get(RefreshToken, row_id)
    if row is None:
        raise RuntimeError("mint_refresh_token did not persist a refresh token row")
    return row
