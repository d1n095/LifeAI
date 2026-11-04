"""Gated remediation proposals. Sensitive changes require Founder approval and are never applied."""

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

SENSITIVE_CLASSES = frozenset(
    {"installation", "permission", "encryption", "authentication", "network", "policy"}
)
SAFE_RESEARCH_CLASSES = frozenset({"code_fix", "documentation", "test_only"})

PROPOSED = "security_remediation_proposed"
APPROVED = "security_remediation_approved"
APPLY_REFUSED = "security_remediation_apply_refused"


class SensitiveChangeRefused(Exception):
    """Raised when a caller asks this module to apply a change it must never apply."""


def propose_remediation(
    db: Session,
    *,
    owner_id: UUID,
    finding_id: str,
    change_class: str,
    title: str,
    summary: str,
    isolated_test_plan: str,
) -> dict[str, Any]:
    change_class = change_class.strip().lower()
    if change_class not in SENSITIVE_CLASSES | SAFE_RESEARCH_CLASSES:
        change_class = "policy"
    proposal_id = str(uuid.uuid4())
    requires_approval = change_class in SENSITIVE_CLASSES
    payload = sanitize_mapping(
        {
            "proposal_id": proposal_id,
            "finding_id": finding_id,
            "change_class": change_class,
            "title": title[:200],
            "summary": summary[:1000],
            "isolated_test_plan": isolated_test_plan[:500],
            "requires_founder_approval": requires_approval,
            "status": "awaiting_founder_approval" if requires_approval else "proposed_research",
            "applied": False,
            "live_checkout_written": False,
        }
    )
    record_audit(
        db,
        user_id=owner_id,
        action=PROPOSED,
        entity_type="security_remediation",
        entity_id=proposal_id,
        detail=json.dumps(payload, sort_keys=True),
    )
    return payload


def list_proposals(db: Session, *, owner_id: UUID) -> list[dict[str, Any]]:
    rows = list(
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.user_id == owner_id,
                AuditLog.action.in_((PROPOSED, APPROVED, APPLY_REFUSED)),
                AuditLog.entity_type == "security_remediation",
            )
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        ).scalars()
    )
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = _parse(row.detail)
        proposal_id = payload.get("proposal_id") or row.entity_id
        if not proposal_id:
            continue
        current = by_id.get(str(proposal_id), payload)
        if row.action == APPROVED:
            current["status"] = "approved_not_applied"
            current["approved"] = True
            current["applied"] = False
        elif row.action == APPLY_REFUSED:
            current["apply_refused"] = True
            current["applied"] = False
        else:
            current.setdefault("status", payload.get("status"))
        by_id[str(proposal_id)] = current
    return list(by_id.values())


def approve_remediation(db: Session, *, owner_id: UUID, proposal_id: str) -> dict[str, Any]:
    existing = _load_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    if existing is None:
        raise KeyError(proposal_id)
    payload = {
        **existing,
        "status": "approved_not_applied",
        "approved": True,
        "applied": False,
        "live_checkout_written": False,
        "note": "Founder approval records intent only. This module never applies the change.",
    }
    record_audit(
        db,
        user_id=owner_id,
        action=APPROVED,
        entity_type="security_remediation",
        entity_id=proposal_id,
        detail=json.dumps(sanitize_mapping(payload), sort_keys=True),
    )
    return payload


def refuse_apply(db: Session, *, owner_id: UUID, proposal_id: str, reason: str) -> dict[str, Any]:
    existing = _load_proposal(db, owner_id=owner_id, proposal_id=proposal_id) or {
        "proposal_id": proposal_id,
        "change_class": "unknown",
    }
    payload = {
        **existing,
        "applied": False,
        "status": "apply_refused",
        "reason": reason,
        "privilege_escalated": False,
    }
    record_audit(
        db,
        user_id=owner_id,
        action=APPLY_REFUSED,
        entity_type="security_remediation",
        entity_id=proposal_id,
        detail=json.dumps(sanitize_mapping(payload), sort_keys=True),
        commit=True,
    )
    raise SensitiveChangeRefused(reason)


def proposals_for_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Advisory proposals derived from findings. Not persisted until the Founder asks."""
    proposals: list[dict[str, Any]] = []
    for finding in findings:
        change_class = _class_for_finding(finding)
        proposals.append(
            {
                "finding_id": finding.get("id"),
                "change_class": change_class,
                "title": f"Remediate {finding.get('kind')}",
                "summary": finding.get("recommended_action"),
                "requires_founder_approval": change_class in SENSITIVE_CLASSES,
                "applied": False,
                "isolated_test_plan": "Re-run backend/tests/backend/test_mainai_security_diagnostics.py in an isolated checkout. Do not write the live server.",
            }
        )
    return proposals


def _class_for_finding(finding: dict[str, Any]) -> str:
    category = str(finding.get("category") or "")
    if category in SENSITIVE_CLASSES:
        return category
    kind = str(finding.get("kind") or "")
    if "dependency" in kind:
        return "installation"
    if "auth" in kind:
        return "authentication"
    if "permission" in kind:
        return "permission"
    if "leak" in kind or "secret" in kind:
        return "authentication"
    return "code_fix"


def _load_proposal(db: Session, *, owner_id: UUID, proposal_id: str) -> dict[str, Any] | None:
    row = db.execute(
        select(AuditLog)
        .where(
            AuditLog.user_id == owner_id,
            AuditLog.action == PROPOSED,
            AuditLog.entity_id == proposal_id,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _parse(row.detail) if row else None


def _parse(detail: str | None) -> dict[str, Any]:
    if not detail:
        return {}
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
