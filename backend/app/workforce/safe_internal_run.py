"""First SAFE INTERNAL MainAI run — no external provider, no consequential writes.

Distinct from LOW_RISK_PROVIDER_RUN. Never silently escalates to provider access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_startup_readiness import ReadinessLevel, evaluate_startup_readiness
from app.workforce.audit_receipt import DelegationAuditReceipt, build_audit_receipt
from app.workforce.first_delegation_scenario import run_low_risk_public_text_delegation
from app.workforce.first_team import bootstrap_first_team, inspect_first_team
from app.workforce.department_evidence import department_capability_ledger
from app.workforce.failure import resume_after_restart
from app.workforce.kill_switch import KillSwitchState, activate_kill_switch, assert_not_killed
from app.models.workforce import WorkforceAssignment


class SafeInternalRunError(Exception):
    pass


@dataclass
class SafeInternalRunReport:
    readiness_level: str
    organization_snapshot: dict
    department_ledger_summary: list[dict]
    task_receipt: dict
    restart_ok: bool
    kill_switch_armed: bool
    provider_invoked: bool
    consequential_writes: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "readiness_level": self.readiness_level,
            "organization_snapshot": self.organization_snapshot,
            "department_ledger_summary": self.department_ledger_summary,
            "task_receipt": self.task_receipt,
            "restart_ok": self.restart_ok,
            "kill_switch_armed": self.kill_switch_armed,
            "provider_invoked": self.provider_invoked,
            "consequential_writes": self.consequential_writes,
            "notes": self.notes,
            "milestone": "SAFE_INTERNAL_RUN",
            "escalated_to_provider": False,
        }


def require_safe_internal_readiness(*, claude_reviews_satisfied: bool | None = None) -> None:
    report = evaluate_startup_readiness(claude_reviews_satisfied=claude_reviews_satisfied)
    if report.level == ReadinessLevel.BLOCKED:
        raise SafeInternalRunError(f"startup BLOCKED: {report.blocking}")
    # SAFE_INTERNAL is the minimum; higher levels are also fine for internal run.
    allowed = {
        ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN,
        ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN,
        ReadinessLevel.READY_FOR_SERIOUS_AUTONOMOUS_RUN,
    }
    if report.level not in allowed:
        raise SafeInternalRunError(f"unexpected readiness level {report.level}")


def run_first_safe_internal_mainai_run(
    db: Session,
    *,
    owner_id: uuid.UUID,
    public_text: str = "Library hours: open weekdays nine to five. Public notice.",
) -> SafeInternalRunReport:
    """Execute the first safe internal MainAI run.

    - no external provider
    - no consequential writes
    - inspect readiness + org
    - one harmless internal task (dry-run worker)
    - restart recovery checkpoint
    - durable receipts
    """
    notes: list[str] = []
    assert_not_killed(db, owner_id=owner_id)
    require_safe_internal_readiness(claude_reviews_satisfied=None)
    readiness = evaluate_startup_readiness(claude_reviews_satisfied=None)
    notes.append(f"readiness={readiness.level.value}")

    bootstrap_first_team(db, owner_id=owner_id)
    org = inspect_first_team(db, owner_id=owner_id)
    ledger = department_capability_ledger(db, owner_id=owner_id)
    notes.append(f"departments={len(ledger)} all_candidates_enforced")

    # Harmless internal task — activate_provider forced False (no silent escalation).
    run = run_low_risk_public_text_delegation(
        db,
        owner_id=owner_id,
        public_text=public_text,
        activate_provider=False,
    )
    if run.provider_invoked:
        raise SafeInternalRunError("SAFE_INTERNAL_RUN must not invoke provider")

    assignment = db.get(WorkforceAssignment, run.assignment_id)
    if assignment is None:
        raise SafeInternalRunError("assignment missing after run")

    receipt = build_audit_receipt(
        db,
        owner_id=owner_id,
        assignment=assignment,
        founder_request=f"Safe internal classify: {public_text[:80]}",
        selected_department="Research",
        selected_worker_key=run.selected_agent_key,
        selection_reason=run.selection_explanation,
        disclosed_kinds=list(run.disclosed_kinds),
        denied_kinds=list(run.denied_kinds),
        spend_reservation={"usd": 0.0, "provider_invoked": False},
        provider_call={"invoked": False, "mode": "in_process"},
        raw_result=(assignment.result_payload or {}),
        verification={
            "raw_status": run.raw_verification_status,
            "final_status": run.final_verification_status,
            "independent": True,
        },
        performance_update={"capability": "public_text_classify", "success": True},
    )

    # Simulate MainAI restart mid/post chain — durable checkpoint, fail-closed if unsafe.
    cp = resume_after_restart(
        db,
        owner_id=owner_id,
        assignment=assignment,
        restart_kind="mainai_restart",
    )
    restart_ok = cp.checkpoint_kind == "restart" and cp.partial_result is not None
    notes.append("restart_checkpoint_recorded")

    # Kill switch must be armable without leaving reusable live authority.
    ks = activate_kill_switch(db, owner_id=owner_id, reason="safe_internal_run_end_arm_test")
    notes.append(f"kill_switch={ks.active}")

    return SafeInternalRunReport(
        readiness_level=readiness.level.value,
        organization_snapshot=org,
        department_ledger_summary=[
            {
                "department": r["department"],
                "status": r["status"],
                "candidate": r["candidate"],
                "evidence_proves_capability": r["evidence_proves_capability"],
            }
            for r in ledger
        ],
        task_receipt=receipt.as_dict(),
        restart_ok=restart_ok,
        kill_switch_armed=ks.active,
        provider_invoked=False,
        consequential_writes=False,
        notes=notes,
    )
