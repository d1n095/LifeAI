"""Delegation Broker (T2) — MainAI never calls arbitrary agents directly.

Resolves a bounded Agent Assignment. The assignment itself grants ZERO extra authority.
Runtime dispatch that depends on unverified safety gates (#218 etc.) is intentionally not
wired here — this module creates durable contracts + authority envelopes only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest, WorkforceTeam
from app.workforce.authority import TaskScopedAuthority, revoke_assignment_authority
from app.workforce.context import create_context_package
from app.workforce.performance import record_job_attempt
from app.workforce.registry import assert_agent_selectable, get_workforce_agent
from app.workforce.selector import select_best_candidate
from app.workforce.types import VERIFICATION_STATUSES


class DelegationBrokerError(Exception):
    pass


class VerificationError(DelegationBrokerError):
    pass


def submit_delegation_request(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_text: str,
    required_capability: str,
    goal_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    risk: str = "low",
    data_sensitivity: str = "internal",
    cost_ceiling_usd: float | None = None,
    latency_preference: str = "balanced",
    verification_requirement: str = "independent_verifier",
    provenance: dict | None = None,
) -> WorkforceDelegationRequest:
    row = WorkforceDelegationRequest(
        owner_id=owner_id,
        goal_id=goal_id,
        task_id=task_id,
        goal_text=goal_text,
        required_capability=required_capability,
        risk=risk,
        data_sensitivity=data_sensitivity,
        cost_ceiling_usd=cost_ceiling_usd,
        latency_preference=latency_preference,
        verification_requirement=verification_requirement,
        status="open",
        provenance=dict(provenance or {}),
    )
    db.add(row)
    db.flush()
    return row


def resolve_delegation(
    db: Session,
    *,
    owner_id: uuid.UUID,
    request: WorkforceDelegationRequest,
    context_items: list[dict] | None = None,
    authority: TaskScopedAuthority | None = None,
    prefer_local_only: bool = False,
    ttl_hours: int = 24,
    verifier_profile_id: uuid.UUID | None = None,
) -> WorkforceAssignment:
    """Broker: select agent → package context → mint task-scoped assignment.

    Does NOT invoke providers. Does NOT grant MainAI's broad authority.
    """
    if request.owner_id != owner_id:
        raise DelegationBrokerError("owner mismatch on delegation request")
    if request.status != "open":
        raise DelegationBrokerError(f"request status={request.status} is not open")

    prefer_local = prefer_local_only or request.data_sensitivity in ("vault", "secret", "high")
    best = select_best_candidate(
        db,
        owner_id=owner_id,
        required_capability=request.required_capability,
        risk=request.risk,
        data_sensitivity=request.data_sensitivity,
        prefer_local_only=prefer_local,
        cost_ceiling_usd=request.cost_ceiling_usd,
    )
    profile = get_workforce_agent(db, owner_id=owner_id, agent_id=best.profile_id)
    assert_agent_selectable(profile)

    if request.verification_requirement == "independent_verifier":
        if verifier_profile_id is None:
            raise DelegationBrokerError("independent_verifier required but verifier_profile_id missing")
        if verifier_profile_id == profile.id:
            raise DelegationBrokerError("BUILDER_CANNOT_SELF_VERIFY: verifier must be a different agent")
        verifier = get_workforce_agent(db, owner_id=owner_id, agent_id=verifier_profile_id)
        assert_agent_selectable(verifier)

    auth = authority or TaskScopedAuthority(
        allowed_read_paths=(),
        allowed_write_paths=(),
        allowed_tool_classes=tuple(profile.allowed_tool_classes or ()),
        allow_execution_effects=False,
        spend_ceiling_usd=request.cost_ceiling_usd,
        expires_at=datetime.utcnow() + timedelta(hours=ttl_hours),
    )

    package = create_context_package(
        db,
        owner_id=owner_id,
        trust_zone=profile.trust_zone,
        requested_items=list(context_items or []),
        provenance={"delegation_request_id": str(request.id), "profile_id": str(profile.id)},
    )

    assignment = WorkforceAssignment(
        owner_id=owner_id,
        delegation_request_id=request.id,
        profile_id=profile.id,
        context_package_id=package.id,
        allowed_read_paths=list(auth.allowed_read_paths),
        allowed_write_paths=list(auth.allowed_write_paths),
        allowed_tool_classes=list(auth.allowed_tool_classes),
        allowed_network_destinations=list(auth.allowed_network_destinations),
        allowed_project_ids=list(auth.allowed_project_ids),
        spend_ceiling_usd=auth.spend_ceiling_usd,
        allow_execution_effects=auth.allow_execution_effects,
        expires_at=auth.expires_at,
        verification_status="UNVERIFIED",
        verifier_profile_id=verifier_profile_id,
        status="assigned",
        result_treated_as_data=True,
        selection_score={"score": best.score, **best.explanation},
        provenance={
            "invariants": [
                "DELEGATION_NE_AUTHORIZATION",
                "AGENT_ROLE_NE_AUTHORITY",
                "EXTERNAL_MODEL_OUTPUT_NE_TRUSTED_FACT",
            ],
            "authority_granted_extra": False,
        },
    )
    db.add(assignment)
    request.status = "assigned"
    request.selection_explanation = {"score": best.score, **best.explanation}
    request.resolved_at = datetime.utcnow()
    profile.last_used_at = datetime.utcnow()
    db.flush()

    record_job_attempt(
        db,
        owner_id=owner_id,
        profile_id=profile.id,
        capability_tag=request.required_capability,
    )
    return assignment


def ingest_untrusted_result(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    payload: dict,
) -> WorkforceAssignment:
    """Store worker output as DATA. Never auto-promotes verification or authority."""
    if assignment.owner_id != owner_id:
        raise DelegationBrokerError("owner mismatch")
    from app.workforce.authority import require_live_assignment_authority
    from app.workforce.injection import scrub_authority_mutations

    require_live_assignment_authority(assignment)
    cleaned, stripped = scrub_authority_mutations(payload)
    assignment.result_payload = {"data": cleaned, "stripped_authority_keys": stripped}
    assignment.result_treated_as_data = True
    assignment.verification_status = "UNVERIFIED"
    assignment.status = "awaiting_verification"
    assignment.updated_at = datetime.utcnow()
    db.flush()
    return assignment


def mark_verification(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    status: str,
    verifier_profile_id: uuid.UUID,
) -> WorkforceAssignment:
    if status not in VERIFICATION_STATUSES:
        raise VerificationError(f"invalid verification status: {status}")
    if assignment.owner_id != owner_id:
        raise DelegationBrokerError("owner mismatch")
    if status == "VERIFIED":
        if verifier_profile_id == assignment.profile_id:
            raise VerificationError("BUILDER_CANNOT_SELF_VERIFY")
        if (
            assignment.verifier_profile_id is not None
            and verifier_profile_id != assignment.verifier_profile_id
        ):
            raise VerificationError("verifier_profile_id does not match assignment requirement")
    assignment.verification_status = status
    assignment.updated_at = datetime.utcnow()
    if status == "VERIFIED":
        assignment.status = "completed"
        assignment.completed_at = datetime.utcnow()
    elif status == "REJECTED":
        assignment.status = "failed"
        assignment.completed_at = datetime.utcnow()
    elif status == "SUPERSEDED":
        assignment.status = "superseded"
        assignment.completed_at = datetime.utcnow()
    db.flush()
    return assignment


def form_team(
    db: Session,
    *,
    owner_id: uuid.UUID,
    name: str,
    pattern: str,
    member_profile_ids: list[uuid.UUID],
) -> WorkforceTeam:
    """T8 — team formation. Members do not share context packages automatically."""
    if len(set(member_profile_ids)) != len(member_profile_ids):
        raise DelegationBrokerError("duplicate team members")
    for pid in member_profile_ids:
        get_workforce_agent(db, owner_id=owner_id, agent_id=pid)
    team = WorkforceTeam(
        owner_id=owner_id,
        name=name,
        pattern=pattern,
        member_profile_ids=[str(x) for x in member_profile_ids],
        status="active",
        provenance={"shared_context_automatic": False},
    )
    db.add(team)
    db.flush()
    return team


def cancel_assignment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    reason: str,
) -> WorkforceAssignment:
    if assignment.owner_id != owner_id:
        raise DelegationBrokerError("owner mismatch")
    revoke_assignment_authority(assignment, reason=reason)
    db.flush()
    return assignment
