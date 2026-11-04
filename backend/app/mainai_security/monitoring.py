"""Threat monitoring over existing audit events. Distinguishes confirmed vs uncertain."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.mainai_security.leak import mask_identity, redact_text

FAILED_LOGIN_ACTIONS = frozenset({"login_failed", "login_blocked_too_many_attempts"})
AUTH_SUCCESS_ACTIONS = frozenset({"login_success", "refresh_success", "logout", "email_verified"})
PRIVILEGE_ACTIONS = frozenset(
    {
        "logout_all_devices",
        "password_reset_success",
        "account_deleted",
        "account_deletion_failed_password",
    }
)
REMOTE_ACTIONS = frozenset({"github_write", "privileged_remote", "prepare_github_pr"})
KNOWN_ACTIONS = FAILED_LOGIN_ACTIONS | AUTH_SUCCESS_ACTIONS | PRIVILEGE_ACTIONS | REMOTE_ACTIONS

CONFIRMED_FAIL_THRESHOLD = 10
UNCERTAIN_FAIL_THRESHOLD = 3
UNUSUAL_IP_THRESHOLD = 4


def monitor_audit_events(
    db: Session,
    *,
    owner_id: UUID | None,
    window_minutes: int = 15,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read existing AuditLog rows only. Forged unknown actions are not treated as incidents."""
    cutoff = (now or datetime.utcnow()) - timedelta(minutes=window_minutes)
    rows = list(
        db.execute(
            select(AuditLog)
            .where(AuditLog.created_at >= cutoff)
            .order_by(AuditLog.created_at.desc())
            .limit(500)
        ).scalars()
    )
    return classify_audit_rows(rows, owner_id=owner_id)


def classify_audit_rows(rows: list[AuditLog], *, owner_id: UUID | None) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    failed_by_identity: dict[str, list[AuditLog]] = defaultdict(list)
    success_ips: dict[str, set[str]] = defaultdict(set)
    privilege_rows: list[AuditLog] = []
    remote_rows: list[AuditLog] = []

    for row in rows:
        if row.action not in KNOWN_ACTIONS:
            continue
        if row.action in FAILED_LOGIN_ACTIONS:
            failed_by_identity[_identity_key(row)].append(row)
        elif row.action in AUTH_SUCCESS_ACTIONS and row.ip_address:
            success_ips[str(row.user_id or row.detail or "unknown")].add(row.ip_address)
        elif row.action in PRIVILEGE_ACTIONS:
            privilege_rows.append(row)
        elif row.action in REMOTE_ACTIONS:
            remote_rows.append(row)

    for identity, events in failed_by_identity.items():
        blocked = any(event.action == "login_blocked_too_many_attempts" for event in events)
        count = len(events)
        if blocked or count >= CONFIRMED_FAIL_THRESHOLD:
            confidence = "confirmed"
            severity = "high"
            kind = "brute_force_or_credential_stuffing"
        elif count >= UNCERTAIN_FAIL_THRESHOLD:
            confidence = "uncertain"
            severity = "medium"
            kind = "repeated_failed_login"
        else:
            confidence = "uncertain"
            severity = "low"
            kind = "isolated_failed_login"
        incidents.append(
            _incident(
                kind=kind,
                confidence=confidence,
                severity=severity,
                source="audit_log",
                component="authentication",
                evidence={
                    "identity": mask_identity(identity if "@" in identity else None) or "masked",
                    "failed_count": count,
                    "blocked": blocked,
                    "ips": sorted({event.ip_address or "unknown" for event in events})[:8],
                    "raw_detail_copied": False,
                },
                recommended_action="Preserve the existing audit rows. Do not disable accounts automatically. Review from the Founder security dashboard.",
            )
        )

    for subject, ips in success_ips.items():
        if len(ips) < UNUSUAL_IP_THRESHOLD:
            continue
        incidents.append(
            _incident(
                kind="unusual_access_pattern",
                confidence="uncertain",
                severity="medium",
                source="audit_log",
                component="authentication",
                evidence={"subject": "masked", "distinct_ips": len(ips), "owner_match": _owner_match(subject, owner_id)},
                recommended_action="Treat as an uncertain signal until the Founder confirms the access pattern.",
            )
        )

    for row in privilege_rows:
        incidents.append(
            _incident(
                kind="privilege_or_credential_change",
                confidence="confirmed",
                severity="medium",
                source="audit_log",
                component="authorization",
                evidence={
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "ip_address": row.ip_address,
                    "detail": redact_text(row.detail)[0] if row.detail else None,
                },
                recommended_action="This is an existing durable audit event. No additional privilege is granted by observing it.",
            )
        )

    for row in remote_rows:
        incidents.append(
            _incident(
                kind="suspicious_remote_operation",
                confidence="confirmed",
                severity="high",
                source="audit_log",
                component="remote_effects",
                evidence={"action": row.action, "entity_type": row.entity_type},
                recommended_action="Remote effects stay off unless the Founder already enabled a write profile. Do not escalate privileges.",
            )
        )
    return incidents


def _identity_key(row: AuditLog) -> str:
    if row.detail:
        return row.detail
    if row.user_id:
        return str(row.user_id)
    return row.ip_address or "unknown"


def _owner_match(subject: str, owner_id: UUID | None) -> bool:
    return owner_id is not None and subject == str(owner_id)


def _incident(**kwargs: Any) -> dict[str, Any]:
    kind = kwargs["kind"]
    identity = str(kwargs.get("evidence", {}).get("identity") or kwargs.get("evidence", {}).get("action") or kind)
    return {
        "id": f"inc_{kind}_{identity}".replace(" ", "_")[:80],
        "status": "open",
        "applied": False,
        "privilege_escalated": False,
        **kwargs,
    }
