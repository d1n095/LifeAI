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
    ip_address: str | None = None,
    commit: bool = True,
) -> None:
    """Write one audit trail entry. Never pass API keys, passwords or tokens as `detail`.

    `request`/`ip_address` are mutually exclusive ways to record the client address:
    `request` (the ordinary router-handler case) extracts it the same way every existing
    caller already relies on; `ip_address` lets a DOMAIN-LAYER caller (one with no FastAPI
    Request object at all, e.g. app/rag/source_purge.py) pass an already-extracted, neutral
    string instead — the router extracts it, the domain service never imports fastapi.

    `commit=False` (Pass 22) adds this row to the session WITHOUT committing — for a caller
    that needs the audit write to be part of its OWN atomic transaction (so a failure
    committing the audit row rolls back the caller's other writes too, and a successful audit
    is never silently lost to a later, separate commit failing). The caller remains
    responsible for eventually committing (or rolling back) in that case."""
    resolved_ip = request.client.host if request and request.client else ip_address
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            ip_address=resolved_ip,
        )
    )
    if commit:
        db.commit()
