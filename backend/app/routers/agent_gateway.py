"""Recipient-facing Agent Messaging Gateway.

Narrowly scoped: a Founder-issued local bridge token may list and claim only that
recipient's inbox and submit receipts. It cannot reach Founder-ops, STOP, GitHub
writes, Recall, or independent certification.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.limiter import limiter
from app.request_context import current_user_id as current_user_id_var

settings = get_settings()

router = APIRouter(prefix="/api/mainai/agent-gateway", tags=["agent-gateway"])


class ReceiptIn(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=64)
    agent_identity: str | None = None
    evidence: dict | None = None
    missing_capability: str | None = None
    independently_verified: bool | None = None
    first_cycle_go: bool | None = None
    github_write: bool | None = None
    override_stop: bool | None = None


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bridge token required")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bridge token required")
    return token


def require_bridge(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> dict:
    from app.agent_coordination.delivery import parse_bridge_token, resolve_bridge

    token = _bearer_token(authorization)
    parsed = parse_bridge_token(token)
    if parsed is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bridge token rejected")
    current_user_id_var.set(str(parsed["owner_id"]))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(parsed["owner_id"])})
    resolved = resolve_bridge(db, token=token)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="bridge token rejected")
    return resolved


@router.get("/inbox")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def list_recipient_inbox(request: Request, bridge: dict = Depends(require_bridge)):
    from app.agent_coordination.delivery import delivery_public_view, list_inbox

    messages = list_inbox(bridge["service"], recipient=bridge["recipient"])
    return {
        "recipient": bridge["recipient"],
        "messages": messages,
        "vendor_delivery_claimed": False,
        "first_cycle_go": False,
        "adapters": delivery_public_view(bridge["service"])["adapters"],
    }


@router.post("/inbox/{message_id}/claim")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def claim_recipient_message(
    request: Request,
    message_id: str,
    db: Session = Depends(get_db),
    bridge: dict = Depends(require_bridge),
):
    from app.agent_coordination.delivery import claim_message, persist_delivery

    result = claim_message(bridge["service"], recipient=bridge["recipient"], message_id=str(message_id))
    if result.get("outcome") == "forged_rejected":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result.get("reason"))
    persist_delivery(db, owner_id=bridge["owner_id"], service=bridge["service"])
    db.commit()
    return result


@router.post("/receipts")
@limiter.limit(f"{settings.rate_limit_default_per_minute}/minute")
def submit_recipient_receipt(
    request: Request,
    payload: ReceiptIn,
    db: Session = Depends(get_db),
    bridge: dict = Depends(require_bridge),
):
    from app.agent_coordination.delivery import submit_receipt

    body = payload.model_dump(exclude_none=True)
    if body.get("agent_identity") is None:
        body["agent_identity"] = bridge["recipient"]
    result = submit_receipt(
        db,
        owner_id=bridge["owner_id"],
        recipient=bridge["recipient"],
        payload=body,
        service=bridge["service"],
    )
    if result.get("outcome") == "forged_rejected":
        db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=result.get("reason"))
    if result.get("outcome") in {"rejected", "stale_rejected"}:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.get("reason"))
    db.commit()
    return result
