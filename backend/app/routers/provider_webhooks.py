"""Narrow provider-authenticated HTTP ingress (no Founder cookie authority)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent_coordination.provider_webhook_ingress import ingest_cursor_webhook
from app.config import get_settings
from app.db import get_db
from app.limiter import limiter
from app.request_context import current_user_id

router = APIRouter(prefix="/api/provider-events", tags=["provider-events"])


@router.post("/cursor/{owner_id}")
@limiter.limit("60/minute")
async def cursor_status_event(owner_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        owner_uuid = uuid.UUID(owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid owner identifier.") from exc
    settings = get_settings()
    secret = settings.cursor_cloud_webhook_secret
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Provider ingress is not configured.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="application/json required.")
    content_length = request.headers.get("content-length")
    if content_length and (not content_length.isdigit() or int(content_length) > settings.provider_webhook_max_body_bytes):
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Webhook body too large.")
    raw_body = await request.body()
    if len(raw_body) > settings.provider_webhook_max_body_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Webhook body too large.")
    signature = request.headers.get("x-webhook-signature", "")
    if not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Provider signature required.")
    token = current_user_id.set(str(owner_uuid))
    try:
        # owner_id is lookup scope only. HMAC + durable assignment binding remains authority.
        db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_uuid)})
        result = ingest_cursor_webhook(
            db,
            owner_id=owner_uuid,
            secret=secret,
            raw_body=raw_body,
            signature=signature,
            user_agent=request.headers.get("user-agent"),
            delivery_id=request.headers.get("x-webhook-id"),
            max_age_seconds=settings.provider_webhook_max_age_seconds,
        )
    finally:
        current_user_id.reset(token)
    if result.get("outcome") == "rejected":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=result.get("reason"))
    return result
