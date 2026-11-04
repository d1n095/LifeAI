"""Incident handling: notify, preserve evidence, recommend recovery. Never escalate privileges."""

from __future__ import annotations

import json
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models.audit import AuditLog
from app.mainai_security.leak import sanitize_mapping

RECORDED = "security_incident_recorded"
ACKNOWLEDGED = "security_incident_acknowledged"


def preserve_incident(
    db: Session,
    *,
    owner_id: UUID,
    incident: dict[str, Any],
) -> dict[str, Any]:
    incident_id = str(incident.get("id") or uuid.uuid4())
    payload = sanitize_mapping(
        {
            "incident_id": incident_id,
            "kind": incident.get("kind"),
            "confidence": incident.get("confidence"),
            "severity": incident.get("severity"),
            "evidence": incident.get("evidence") or {},
            "notification": "founder_dashboard_only",
            "privilege_escalated": False,
            "irreversible_change": False,
            "recovery": _recovery_for(incident),
            "rollback": _rollback_for(incident),
            "status": "preserved",
        }
    )
    record_audit(
        db,
        user_id=owner_id,
        action=RECORDED,
        entity_type="security_incident",
        entity_id=incident_id,
        detail=json.dumps(payload, sort_keys=True),
    )
    return payload


def acknowledge_incident(db: Session, *, owner_id: UUID, incident_id: str) -> dict[str, Any]:
    payload = {
        "incident_id": incident_id,
        "status": "acknowledged",
        "privilege_escalated": False,
        "applied": False,
    }
    record_audit(
        db,
        user_id=owner_id,
        action=ACKNOWLEDGED,
        entity_type="security_incident",
        entity_id=incident_id,
        detail=json.dumps(payload, sort_keys=True),
    )
    return payload


def list_preserved_incidents(db: Session, *, owner_id: UUID) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.user_id == owner_id,
                AuditLog.action.in_((RECORDED, ACKNOWLEDGED)),
                AuditLog.entity_type == "security_incident",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        ).scalars()
    )
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row.detail or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        incident_id = str(payload.get("incident_id") or row.entity_id)
        current = by_id.get(incident_id, payload)
        if row.action == ACKNOWLEDGED:
            current["status"] = "acknowledged"
        by_id[incident_id] = current
    return list(by_id.values())


def _recovery_for(incident: dict[str, Any]) -> str:
    kind = str(incident.get("kind") or "")
    if "login" in kind or "brute" in kind:
        return "Keep the existing lockout. Rotate only after the Founder confirms the account is compromised."
    if "remote" in kind:
        return "Leave remote writes off. Inspect the existing audit row. Do not enable GitHub writes from this module."
    if "privilege" in kind:
        return "The observed event already happened. Do not grant additional roles or tokens in response."
    return "Preserve evidence and wait for Founder review. No autonomous recovery action is taken."


def _rollback_for(incident: dict[str, Any]) -> str:
    if incident.get("confidence") != "confirmed":
        return "Uncertain signal — no rollback is planned because no change was applied."
    return "Rollback is advisory only. This module does not revert settings, permissions or deployments."
