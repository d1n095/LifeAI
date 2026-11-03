"""MainAI executive business-workflow engine.

Composes the EXISTING workforce substrate into one end-to-end path:

  objective → persistent plan → team of specialists → per-task delegation with task-scoped
  authority → execution (pluggable runner) → independent verification → performance evidence →
  dependency-ordered aggregation → Founder-approval gate on consequential actions.

Execution is a pluggable `WorkerRunner` (see `app.mainai_business.execution`): the default
`InProcessRunner` is deterministic and provider-free; `ModelBackedRunner` uses REAL local-model
inference (#334) with schema-validated outputs and a deterministic independent validator. A
worker failure (provider unreachable, unparseable output, or a failed validator) fails the
task honestly and never fabricates a result or bypasses verification.

No new tables: every task is a real, persisted, RLS-scoped `WorkforceDelegationRequest` +
`WorkforceAssignment`, stamped with a shared `workflow_id` in provenance so the whole project
is reassembled from the database alone (see `app.mainai_business.view`). No consequential
effect is produced without an explicit Founder approval token.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_business.execution import InProcessRunner
from app.mainai_business.objective import BusinessObjectiveSpec, topological_order
from app.mainai_business.validators import validate_output
from app.models.workforce import (
    WorkforceAgentProfile,
    WorkforceAssignment,
    WorkforceContextPackage,
    WorkforceDelegationRequest,
)
from app.workforce.authority import TaskScopedAuthority
from app.workforce.broker import (
    form_team,
    ingest_untrusted_result,
    resolve_delegation,
    submit_delegation_request,
)
from app.workforce.performance import record_verified_outcome
from app.workforce.registry import register_workforce_agent
from app.workforce.verification import apply_verification_decision

WORKFLOW_PROVENANCE_KEY = "business_workflow_id"


class BusinessWorkflowError(Exception):
    pass


@dataclass
class TaskRecord:
    task_key: str
    title: str
    capability: str
    status: str
    depends_on: tuple[str, ...]
    consequential: bool
    request_id: str | None = None
    assignment_id: str | None = None
    selected_agent_key: str | None = None
    verifier_agent_key: str | None = None
    verification_status: str | None = None
    disclosed_kinds: tuple[str, ...] = ()
    denied_kinds: tuple[str, ...] = ()
    output: dict | None = None
    blocked_reason: str | None = None
    runtime: str | None = None
    receipt: dict | None = None
    failure_reason: str | None = None
    failure_kind: str | None = None  # "authorization" | "output_quality" | None
    correction_attempts: int = 0

    def as_dict(self) -> dict:
        return {
            "task_key": self.task_key,
            "title": self.title,
            "capability": self.capability,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "consequential": self.consequential,
            "request_id": self.request_id,
            "assignment_id": self.assignment_id,
            "selected_agent_key": self.selected_agent_key,
            "verifier_agent_key": self.verifier_agent_key,
            "verification_status": self.verification_status,
            "disclosed_kinds": list(self.disclosed_kinds),
            "denied_kinds": list(self.denied_kinds),
            "output": self.output,
            "blocked_reason": self.blocked_reason,
            "runtime": self.runtime,
            "receipt": self.receipt,
            "failure_reason": self.failure_reason,
            "failure_kind": self.failure_kind,
            "correction_attempts": self.correction_attempts,
        }


@dataclass
class BusinessWorkflowReport:
    workflow_id: str
    objective_key: str
    objective_text: str
    team: list[dict]
    tasks: list[TaskRecord] = field(default_factory=list)
    assessment: dict | None = None
    approvals_required: list[str] = field(default_factory=list)
    approvals_satisfied: list[str] = field(default_factory=list)
    blocked_tasks: list[str] = field(default_factory=list)
    provider_invoked: bool = False
    status: str = "in_progress"  # completed | awaiting_approval | failed | in_progress
    final_deliverable: dict | None = None

    def as_dict(self) -> dict:
        return {
            "workflow_id": self.workflow_id,
            "objective_key": self.objective_key,
            "objective_text": self.objective_text,
            "team": self.team,
            "tasks": [t.as_dict() for t in self.tasks],
            "assessment": self.assessment,
            "approvals_required": self.approvals_required,
            "approvals_satisfied": self.approvals_satisfied,
            "blocked_tasks": self.blocked_tasks,
            "provider_invoked": self.provider_invoked,
            "status": self.status,
            "final_deliverable": self.final_deliverable,
        }


def provision_business_team(
    db: Session, *, owner_id: uuid.UUID, spec: BusinessObjectiveSpec
) -> dict[str, Any]:
    """Register the specialist seats + the independent verifier and form a team.

    Registration grants ZERO authority (see registry). Agents start selectable (`active`) but
    with NO allowed_tool_classes and NO performance evidence — authority is minted per task.
    """
    capability_to_profile: dict[str, WorkforceAgentProfile] = {}
    members: list[uuid.UUID] = []
    team_view: list[dict] = []
    for role in spec.roles:
        profile = register_workforce_agent(
            db,
            owner_id=owner_id,
            agent_key=role.agent_key,
            name=role.name,
            role=role.role,
            agent_type=role.agent_type,
            provider_type="local",
            trust_zone=role.trust_zone,
            capability_tags=[role.capability],
            allowed_tool_classes=(),  # authority is per-task, never on the profile
            cost_class="low",
            status="active",
            provenance={"business_role": True, "objective_key": spec.objective_key},
        )
        capability_to_profile[role.capability] = profile
        members.append(profile.id)
        team_view.append(
            {"agent_key": role.agent_key, "name": role.name, "role": role.role, "capability": role.capability}
        )
    verifier = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=spec.verifier.agent_key,
        name=spec.verifier.name,
        role=spec.verifier.role,
        agent_type=spec.verifier.agent_type,
        provider_type="local",
        trust_zone=spec.verifier.trust_zone,
        capability_tags=[spec.verifier.capability],
        allowed_tool_classes=(),
        cost_class="low",
        status="active",
        provenance={"business_role": True, "verifier": True, "objective_key": spec.objective_key},
    )
    members.append(verifier.id)
    team = form_team(
        db,
        owner_id=owner_id,
        name=f"{spec.objective_key}-team",
        pattern="executive_departments",
        member_profile_ids=members,
    )
    return {
        "team_id": team.id,
        "capability_to_profile": capability_to_profile,
        "verifier": verifier,
        "team_view": team_view,
    }


def _find_existing_request(
    db: Session, *, owner_id: uuid.UUID, workflow_id: str, task_key: str
) -> WorkforceDelegationRequest | None:
    rows = db.execute(
        select(WorkforceDelegationRequest).where(WorkforceDelegationRequest.owner_id == owner_id)
    ).scalars()
    for r in rows:
        prov = r.provenance or {}
        if prov.get(WORKFLOW_PROVENANCE_KEY) == workflow_id and prov.get("task_key") == task_key:
            return r
    return None


def _stamp_receipt(assignment: WorkforceAssignment, receipt: dict) -> None:
    """Attach the execution receipt to the assignment's provenance (durable evidence)."""
    prov = dict(assignment.provenance or {})
    prov["execution_receipt"] = receipt
    assignment.provenance = prov


def _verified_assignment_for_request(
    db: Session, *, owner_id: uuid.UUID, request_id: uuid.UUID
) -> WorkforceAssignment | None:
    return db.execute(
        select(WorkforceAssignment)
        .where(
            WorkforceAssignment.owner_id == owner_id,
            WorkforceAssignment.delegation_request_id == request_id,
            WorkforceAssignment.verification_status == "VERIFIED",
        )
        .order_by(WorkforceAssignment.created_at.desc())
    ).scalars().first()


def run_business_workflow(
    db: Session,
    *,
    owner_id: uuid.UUID,
    spec: BusinessObjectiveSpec,
    approvals: dict[str, str] | None = None,
    workflow_id: str | None = None,
    runner=None,
) -> BusinessWorkflowReport:
    """Drive the objective to completion through the real workforce substrate.

    `runner` selects the execution strategy (default `InProcessRunner`; pass a
    `ModelBackedRunner` for real local-model inference). Idempotent per (workflow_id,
    task_key): a task already carried to VERIFIED is reused, not re-executed — so re-invoking
    after a restart resumes rather than duplicates. Consequential tasks are fail-closed:
    without `approvals[task_key]` they are recorded blocked, their authority is never minted
    and their worker never runs. A worker or validator failure fails that task and blocks its
    dependents — never a fabricated result.
    """
    spec.validate()
    approvals = dict(approvals or {})
    workflow_id = workflow_id or str(uuid.uuid4())
    runner = runner or InProcessRunner()

    provisioned = provision_business_team(db, owner_id=owner_id, spec=spec)
    cap_to_profile = provisioned["capability_to_profile"]
    verifier = provisioned["verifier"]

    report = BusinessWorkflowReport(
        workflow_id=workflow_id,
        objective_key=spec.objective_key,
        objective_text=spec.objective_text,
        team=provisioned["team_view"],
        approvals_required=list(spec.consequential_task_keys()),
    )
    verified_outputs: dict[str, dict] = {}

    for idx, task in enumerate(topological_order(spec)):
        record = TaskRecord(
            task_key=task.task_key,
            title=task.title,
            capability=task.capability,
            status="pending",
            depends_on=task.depends_on,
            consequential=task.consequential,
        )

        # Resume: reuse an already-verified task (restart / duplicate-run safety).
        existing = _find_existing_request(db, owner_id=owner_id, workflow_id=workflow_id, task_key=task.task_key)
        if existing is not None:
            done = _verified_assignment_for_request(db, owner_id=owner_id, request_id=existing.id)
            if done is not None:
                data = (done.result_payload or {}).get("data") or {}
                verified_outputs[task.task_key] = data
                record.status = "verified_reused"
                record.request_id = str(existing.id)
                record.assignment_id = str(done.id)
                record.verification_status = done.verification_status
                record.output = data
                report.tasks.append(record)
                continue

        # Founder-approval gate for consequential actions — fail-closed.
        if task.consequential and not approvals.get(task.task_key):
            req = existing or submit_delegation_request(
                db,
                owner_id=owner_id,
                goal_text=f"[{workflow_id}] {task.title}",
                required_capability=task.capability,
                risk=task.risk,
                data_sensitivity=task.data_sensitivity,
                cost_ceiling_usd=0.0,
                verification_requirement="independent_verifier",
                provenance={
                    WORKFLOW_PROVENANCE_KEY: workflow_id,
                    "task_key": task.task_key,
                    "objective_key": spec.objective_key,
                    "depends_on": list(task.depends_on),
                    "stage": idx,
                    "consequential": True,
                    "blocked": True,
                    "blocked_reason": "awaiting_founder_approval",
                },
            )
            record.status = "blocked_awaiting_founder_approval"
            record.request_id = str(req.id)
            record.blocked_reason = "awaiting_founder_approval"
            record.failure_kind = "authorization"  # NOT an output-quality failure
            report.blocked_tasks.append(task.task_key)
            report.tasks.append(record)
            continue

        # Dependency-readiness gate — a failed/blocked upstream task blocks its dependents
        # honestly; the engine never fabricates a result for an unmet dependency.
        unmet = [dep for dep in task.depends_on if dep not in verified_outputs]
        if unmet:
            record.status = "blocked_unmet_dependency"
            record.blocked_reason = f"unmet_dependency: {','.join(unmet)}"
            record.depends_on = task.depends_on
            report.blocked_tasks.append(task.task_key)
            report.tasks.append(record)
            continue

        # Upstream dependency data flows through the broker's context package (as DATA).
        context_items = []
        inputs: dict[str, dict] = {}
        for dep in task.depends_on:
            dep_out = verified_outputs[dep]
            inputs[dep] = dep_out
            context_items.append(
                {"kind": "excerpt", "excerpt": dep_out, "ref": f"workflow:{workflow_id}:{dep}"}
            )

        task_provenance = {
            WORKFLOW_PROVENANCE_KEY: workflow_id,
            "task_key": task.task_key,
            "objective_key": spec.objective_key,
            "depends_on": list(task.depends_on),
            "stage": idx,
            "consequential": task.consequential,
        }
        if existing is not None and existing.status == "open":
            # Reuse the persisted request (e.g. the blocked consequential request now that it is
            # approved) rather than creating a duplicate for the same (workflow_id, task_key) —
            # one durable request per task keeps the reassembled view unambiguous.
            req = existing
            req.provenance = task_provenance  # clears the prior 'blocked' marker
        else:
            req = submit_delegation_request(
                db,
                owner_id=owner_id,
                goal_text=f"[{workflow_id}] {task.title}",
                required_capability=task.capability,
                risk=task.risk,
                data_sensitivity=task.data_sensitivity,
                cost_ceiling_usd=0.0,
                verification_requirement="independent_verifier",
                provenance=task_provenance,
            )
        authority = TaskScopedAuthority(
            allowed_read_paths=task.allowed_read_paths,
            allowed_write_paths=(),
            allowed_tool_classes=task.allowed_tool_classes,
            allow_execution_effects=False,
            spend_ceiling_usd=0.0,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        assignment = resolve_delegation(
            db,
            owner_id=owner_id,
            request=req,
            context_items=context_items,
            authority=authority,
            prefer_local_only=True,
            verifier_profile_id=verifier.id,
        )

        selected = db.get(WorkforceAgentProfile, assignment.profile_id)
        expected = cap_to_profile.get(task.capability)
        if expected is not None and selected.id != expected.id:
            # Selector must choose the specialist whose capability matches — never a stray seat.
            raise BusinessWorkflowError(
                f"selector picked {selected.agent_key!r} for {task.capability!r}; expected {expected.agent_key!r}"
            )

        pkg = db.get(WorkforceContextPackage, assignment.context_package_id)
        record.request_id = str(req.id)
        record.assignment_id = str(assignment.id)
        record.selected_agent_key = selected.agent_key
        record.verifier_agent_key = verifier.agent_key
        record.disclosed_kinds = tuple(i.get("kind") for i in (pkg.items or []))
        record.denied_kinds = tuple(pkg.denied_kinds or [])

        # Execute via the configured runner, with BOUNDED, AUTHORIZED validator-feedback
        # recovery for model-backed output. Each correction attempt is a fresh real inference
        # re-prompted with the validator's rejection reason — it re-validates from scratch and
        # never bypasses validation or fabricates success. In-process output has no validator
        # retries (deterministic). The outcome of the loop is one of: task_failed (unparseable),
        # task_rejected (validator never satisfied within budget), or accepted (VERIFIED).
        max_corrections = int(getattr(runner, "correction_attempts", 0) or 0)
        feedback: str | None = None
        corrections_used = 0
        exec_result = None
        verify_reason = None
        outcome = None  # "failed" | "rejected" | "accepted"
        while True:
            exec_result = runner.run(
                db, owner_id=owner_id, assignment=assignment, task=task,
                objective_text=spec.objective_text, feedback=feedback,
            )
            if exec_result.receipt.get("provider_invoked"):
                report.provider_invoked = True
            record.runtime = exec_result.receipt.get("runtime")
            record.receipt = exec_result.receipt
            record.correction_attempts = corrections_used
            _stamp_receipt(assignment, exec_result.receipt)

            if not exec_result.ok or exec_result.data is None:
                # Worker could not produce a parseable result — a provider/schema failure is
                # not a validator rejection, so it is not retried with feedback here (the runner
                # already applied its own bounded schema-parse retries).
                outcome = "failed"
                break

            ingest_untrusted_result(db, owner_id=owner_id, assignment=assignment, payload=exec_result.data)
            stored = (assignment.result_payload or {}).get("data") or {}

            if exec_result.validator_ref is None:
                verify_reason = f"independent verifier accepted {task.task_key}"
                outcome = "accepted"
                break

            ok, reason = validate_output(task.capability, stored, inputs)
            if ok:
                verify_reason = f"deterministic validator accepted {task.task_key}: {reason}"
                outcome = "accepted"
                break
            if corrections_used < max_corrections:
                corrections_used += 1
                feedback = reason  # authorized, bounded correction attempt
                continue
            record.failure_reason = reason
            outcome = "rejected"
            break

        if outcome == "failed":
            assignment.result_payload = {"data": None, "execution_failure": exec_result.receipt}
            apply_verification_decision(
                db, owner_id=owner_id, assignment=assignment, decision="REJECTED",
                risk=task.risk, verifier_profile_id=verifier.id,
                reason=f"worker failed {task.task_key}: {exec_result.failure_reason}",
            )
            record_verified_outcome(
                db, owner_id=owner_id, profile_id=assignment.profile_id,
                capability_tag=task.capability, success=False,
            )
            record.status = "failed"
            record.verification_status = assignment.verification_status
            record.failure_reason = exec_result.failure_reason
            record.failure_kind = "output_quality"
            report.tasks.append(record)
            continue

        if outcome == "rejected":
            apply_verification_decision(
                db, owner_id=owner_id, assignment=assignment, decision="REJECTED",
                risk=task.risk, verifier_profile_id=verifier.id,
                deterministic_validator=exec_result.validator_ref,
                reason=f"validator rejected {task.task_key} after {corrections_used} correction(s): {record.failure_reason}",
            )
            record_verified_outcome(
                db, owner_id=owner_id, profile_id=assignment.profile_id,
                capability_tag=task.capability, success=False,
            )
            record.status = "rejected_by_validator"
            record.verification_status = assignment.verification_status
            record.failure_kind = "output_quality"
            report.tasks.append(record)
            continue

        apply_verification_decision(
            db,
            owner_id=owner_id,
            assignment=assignment,
            decision="VERIFIED",
            risk=task.risk,
            verifier_profile_id=verifier.id,
            deterministic_validator=exec_result.validator_ref,
            founder_approval_ref=approvals.get(task.task_key),
            reason=verify_reason,
        )
        record_verified_outcome(
            db,
            owner_id=owner_id,
            profile_id=assignment.profile_id,
            capability_tag=task.capability,
            success=True,
            quality_score=float(stored.get("confidence") or stored.get("blended_confidence") or 1.0),
        )

        verified_outputs[task.task_key] = stored
        record.status = "verified"
        record.verification_status = assignment.verification_status
        record.output = stored
        if task.consequential:
            report.approvals_satisfied.append(task.task_key)
        report.tasks.append(record)

    # Aggregate the Founder-facing assessment from verified outputs only.
    report.assessment = _aggregate_assessment(spec, verified_outputs)
    report.status, report.final_deliverable = _derive_status(spec, report, verified_outputs)
    return report


def publication_document(pub: dict | None) -> dict | None:
    """The final deliverable document from a publication output — handling both the in-process
    shape (`{"document": {...}}`) and the model-backed flat shape (`{title,recommendation,summary}`)."""
    if not pub:
        return None
    doc = pub.get("document")
    if doc:
        return doc
    flat = {k: pub[k] for k in ("title", "recommendation", "summary") if k in pub}
    return flat or None


def _derive_status(
    spec: BusinessObjectiveSpec, report: BusinessWorkflowReport, verified_outputs: dict[str, dict]
) -> tuple[str, dict | None]:
    """Workflow completion state for the Founder. The final deliverable is the verified
    publication output — present (and durably persisted as that assignment's result) only when
    every task, including the consequential publish, passed validation."""
    statuses = {t.task_key: t.status for t in report.tasks}
    all_done = all(s in ("verified", "verified_reused") for s in statuses.values())
    any_blocked = any(s == "blocked_awaiting_founder_approval" for s in statuses.values())
    any_quality_fail = any(
        s in ("failed", "rejected_by_validator", "blocked_unmet_dependency") for s in statuses.values()
    )
    deliverable = publication_document(verified_outputs.get("publish_assessment"))
    if all_done and deliverable is not None:
        return "completed", deliverable
    if any_quality_fail:
        return "failed", None
    if any_blocked:
        return "awaiting_approval", None
    return "in_progress", None


def _aggregate_assessment(spec: BusinessObjectiveSpec, verified_outputs: dict[str, dict]) -> dict:
    synthesis = verified_outputs.get("opportunity_synthesis")
    publication = verified_outputs.get("publish_assessment")
    return {
        "objective_key": spec.objective_key,
        "recommendation": (synthesis or {}).get("recommendation"),
        "conditions": (synthesis or {}).get("conditions"),
        "confidence": (synthesis or {}).get("blended_confidence"),
        "synthesis_available": synthesis is not None,
        "published": publication is not None,
        "published_document": publication_document(publication),
        "contributing_tasks": sorted(verified_outputs.keys()),
    }
