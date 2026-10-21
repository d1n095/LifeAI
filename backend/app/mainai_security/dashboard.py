"""Founder-facing security diagnostics snapshot. Not a first-cycle blocker."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.mainai_security.analysis import collect_analysis_findings
from app.mainai_security.incidents import list_preserved_incidents
from app.mainai_security.leak import sanitize_mapping
from app.mainai_security.monitoring import monitor_audit_events
from app.mainai_security.remediation import list_proposals, proposals_for_findings
from app.mainai_security.restrictions import explain_restrictions

SEVERITY_ORDER = ("critical", "high", "medium", "low", "informational")


def build_security_dashboard(
    db: Session,
    *,
    owner_id: UUID,
    recall_http_mounted: bool = False,
    analysis_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings = collect_analysis_findings(**(analysis_overrides or {}))
    incidents = monitor_audit_events(db, owner_id=owner_id)
    restrictions = explain_restrictions(recall_http_mounted=recall_http_mounted)
    proposals = list_proposals(db, owner_id=owner_id)
    if not proposals:
        proposals = proposals_for_findings(findings)
    preserved = list_preserved_incidents(db, owner_id=owner_id)
    payload = {
        "generated_from": "existing_settings_audit_and_local_manifests",
        "customer_isolation": "mainai_strictly_separate",
        "first_cycle_go": False,
        "blocks_first_cycle": False,
        "fabricated_progress": False,
        "invented_policy": False,
        "autonomous_privilege_escalation": False,
        "findings": findings,
        "incidents": incidents,
        "preserved_incidents": preserved,
        "restrictions": restrictions,
        "remediation": proposals,
        "severity_counts": _counts(findings + incidents, "severity", SEVERITY_ORDER),
        "confidence_counts": _counts(incidents, "confidence", ("confirmed", "uncertain")),
        "limitations": [
            "Does not run live npm audit or paid provider calls.",
            "Does not install packages or change security policy.",
            "Does not apply remediations to the live checkout.",
            "Does not invent restriction explanations.",
            "Does not treat this module as a first-cycle GO input.",
        ],
    }
    return sanitize_mapping(payload)


def security_summary_for_ops(
    db: Session,
    *,
    owner_id: UUID,
    recall_http_mounted: bool = False,
) -> dict[str, Any]:
    dashboard = build_security_dashboard(db, owner_id=owner_id, recall_http_mounted=recall_http_mounted)
    confirmed = sum(1 for item in dashboard["incidents"] if item.get("confidence") == "confirmed")
    return {
        "available": True,
        "finding_count": len(dashboard["findings"]),
        "confirmed_incident_count": confirmed,
        "uncertain_incident_count": len(dashboard["incidents"]) - confirmed,
        "first_cycle_go": False,
        "blocks_first_cycle": False,
        "dashboard_path": "/admin/security",
    }


def _counts(items: list[dict[str, Any]], field: str, order: tuple[str, ...]) -> dict[str, int]:
    counts = {key: 0 for key in order}
    for item in items:
        key = str(item.get(field) or "")
        if key in counts:
            counts[key] += 1
    return counts
