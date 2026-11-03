"""Founder-facing projection of a business workflow — reassembled from persisted rows only.

This reads nothing from memory: given a `workflow_id`, it rebuilds the whole project (plan,
per-task delegation, scoped authority, disclosure, verification, evidence, aggregated
assessment) purely from the owner-scoped database. That is what proves the workflow is
durable and survives a restart, and it is the single interface the Founder reads.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_business.engine import WORKFLOW_PROVENANCE_KEY
from app.models.workforce import (
    WorkforceAgentProfile,
    WorkforceAssignment,
    WorkforceContextPackage,
    WorkforceDelegationRequest,
)
from app.models.workforce_ops import WorkforceVerificationDecision


def _agent_key(db: Session, profile_id) -> str | None:
    if profile_id is None:
        return None
    prof = db.get(WorkforceAgentProfile, profile_id)
    return prof.agent_key if prof else None


def assemble_business_project_view(db: Session, *, owner_id: uuid.UUID, workflow_id: str) -> dict:
    """Rebuild the project view from persisted delegation requests + assignments."""
    requests = [
        r
        for r in db.execute(
            select(WorkforceDelegationRequest).where(WorkforceDelegationRequest.owner_id == owner_id)
        ).scalars()
        if (r.provenance or {}).get(WORKFLOW_PROVENANCE_KEY) == workflow_id
    ]
    requests.sort(key=lambda r: (r.provenance or {}).get("stage", 0))

    tasks: list[dict] = []
    verified_outputs: dict[str, dict] = {}
    objective_key = None
    blocked: list[str] = []
    approvals_required: list[str] = []
    approvals_satisfied: list[str] = []

    for req in requests:
        prov = req.provenance or {}
        objective_key = objective_key or prov.get("objective_key")
        task_key = prov.get("task_key")
        consequential = bool(prov.get("consequential"))
        if consequential:
            approvals_required.append(task_key)

        assignment = db.execute(
            select(WorkforceAssignment)
            .where(
                WorkforceAssignment.owner_id == owner_id,
                WorkforceAssignment.delegation_request_id == req.id,
            )
            .order_by(WorkforceAssignment.created_at.desc())
        ).scalars().first()

        task_view: dict = {
            "task_key": task_key,
            "stage": prov.get("stage"),
            "depends_on": prov.get("depends_on", []),
            "consequential": consequential,
            "request_status": req.status,
            "assignment_id": None,
            "selected_agent_key": None,
            "verifier_agent_key": None,
            "verification_status": None,
            "disclosed_kinds": [],
            "denied_kinds": [],
            "authority": None,
            "output": None,
            "verification_decisions": [],
        }

        if prov.get("blocked"):
            task_view["status"] = "blocked_awaiting_founder_approval"
            blocked.append(task_key)
        elif assignment is None:
            task_view["status"] = "pending"
        else:
            data = (assignment.result_payload or {}).get("data") or {}
            if assignment.verification_status == "VERIFIED":
                verified_outputs[task_key] = data
                if consequential:
                    approvals_satisfied.append(task_key)
            pkg = db.get(WorkforceContextPackage, assignment.context_package_id)
            decisions = db.execute(
                select(WorkforceVerificationDecision)
                .where(
                    WorkforceVerificationDecision.owner_id == owner_id,
                    WorkforceVerificationDecision.assignment_id == assignment.id,
                )
                .order_by(WorkforceVerificationDecision.created_at.asc())
            ).scalars().all()
            task_view.update(
                {
                    "status": assignment.status,
                    "assignment_id": str(assignment.id),
                    "selected_agent_key": _agent_key(db, assignment.profile_id),
                    "verifier_agent_key": _agent_key(db, assignment.verifier_profile_id),
                    "verification_status": assignment.verification_status,
                    "disclosed_kinds": [i.get("kind") for i in (pkg.items if pkg else [])],
                    "denied_kinds": list(pkg.denied_kinds if pkg else []),
                    "authority": {
                        "allowed_read_paths": list(assignment.allowed_read_paths or []),
                        "allowed_tool_classes": list(assignment.allowed_tool_classes or []),
                        "allow_execution_effects": assignment.allow_execution_effects,
                        "spend_ceiling_usd": assignment.spend_ceiling_usd,
                        "revoked_at": assignment.revoked_at.isoformat() if assignment.revoked_at else None,
                    },
                    "output": data,
                    "verification_decisions": [
                        {
                            "decision": d.decision,
                            "verifier_profile_id": str(d.verifier_profile_id) if d.verifier_profile_id else None,
                            "founder_approval_ref": d.founder_approval_ref,
                        }
                        for d in decisions
                    ],
                }
            )
        tasks.append(task_view)

    synthesis = verified_outputs.get("opportunity_synthesis")
    publication = verified_outputs.get("publish_assessment")
    return {
        "workflow_id": workflow_id,
        "objective_key": objective_key,
        "exists": bool(requests),
        "tasks": tasks,
        "approvals_required": approvals_required,
        "approvals_satisfied": approvals_satisfied,
        "blocked_tasks": blocked,
        "assessment": {
            "recommendation": (synthesis or {}).get("recommendation"),
            "conditions": (synthesis or {}).get("conditions"),
            "confidence": (synthesis or {}).get("blended_confidence"),
            "published": publication is not None,
            "published_document": (publication or {}).get("document"),
            "contributing_tasks": sorted(verified_outputs.keys()),
        },
    }
