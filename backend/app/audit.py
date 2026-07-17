import uuid

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.audit import AuditLog


def record_audit(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: str | None = None,
    request: Request | None = None,
) -> None:
    """Write one audit trail entry. Never pass API keys, passwords or tokens as `detail`."""
    ip_address = request.client.host if request and request.client else None
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            ip_address=ip_address,
        )
    )
    db.commit()
