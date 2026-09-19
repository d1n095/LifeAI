"""Failure / takeover / resume (T13).

UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT.
Retry only when safe (none_proven). Alternate-agent takeover preserves partial evidence
and never widens authority.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAssignment, WorkforceDelegationRequest
from app.models.workforce_ops import WorkforceAssignmentCheckpoint
from app.workforce.authority import TaskScopedAuthority, assignment_authority_is_live, revoke_assignment_authority
from app.workforce.broker import DelegationBrokerError
from app.workforce.context import create_context_package
from app.workforce.registry import assert_agent_selectable, get_workforce_agent


class FailureTakeoverError(Exception):
    pass


SAFE_RETRY_EFFECT_STATES = frozenset({"none_known", "none_proven"})
CONSEQUENTIAL_BLOCK_STATES = frozenset({"unknown", "effect_proven"})

FAILURE_CLASSES = frozenset(
    {
        "agent_crash",
        "provider_timeout",
        "provider_rate_limit",
        "malformed_output",
        "partial_result",
        "lost_lease",
        "expired_assignment",
        "revoked_agent",
        "retired_agent",
        "mainai_restart",
        "worker_restart",
    }
)


def _next_sequence(db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID) -> int:
    current = db.execute(
        select(func.coalesce(func.max(WorkforceAssignmentCheckpoint.sequence), 0)).where(
            WorkforceAssignmentCheckpoint.owner_id == owner_id,
            WorkforceAssignmentCheckpoint.assignment_id == assignment_id,
        )
    ).scalar_one()
    return int(current) + 1


def record_checkpoint(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    checkpoint_kind: str,
    partial_result: dict | None = None,
    evidence_refs: list | None = None,
    external_effect_state: str = "none_known",
    failure_class: str | None = None,
    recoverable: bool = True,
    provenance: dict | None = None,
) -> WorkforceAssignmentCheckpoint:
    if assignment.owner_id != owner_id:
        raise FailureTakeoverError("owner mismatch")
    if failure_class is not None and failure_class not in FAILURE_CLASSES:
        raise FailureTakeoverError(f"unknown failure_class: {failure_class}")
    if external_effect_state not in ("none_known", "none_proven", "unknown", "effect_proven"):
        raise FailureTakeoverError(f"invalid external_effect_state: {external_effect_state}")

    row = WorkforceAssignmentCheckpoint(
        owner_id=owner_id,
        assignment_id=assignment.id,
        checkpoint_kind=checkpoint_kind,
        sequence=_next_sequence(db, owner_id=owner_id, assignment_id=assignment.id),
        partial_result=dict(partial_result or {}),
        evidence_refs=list(evidence_refs or []),
        external_effect_state=external_effect_state,
        failure_class=failure_class,
        recoverable=recoverable,
        provenance=dict(provenance or {}),
    )
    db.add(row)
    assignment.external_effect_state = external_effect_state
    if failure_class:
        assignment.failure_class = failure_class
    assignment.updated_at = datetime.utcnow()
    db.flush()
    return row


def latest_checkpoint(
    db: Session, *, owner_id: uuid.UUID, assignment_id: uuid.UUID
) -> WorkforceAssignmentCheckpoint | None:
    return db.execute(
        select(WorkforceAssignmentCheckpoint)
        .where(
            WorkforceAssignmentCheckpoint.owner_id == owner_id,
            WorkforceAssignmentCheckpoint.assignment_id == assignment_id,
        )
        .order_by(WorkforceAssignmentCheckpoint.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()


def can_safely_retry(assignment: WorkforceAssignment) -> tuple[bool, str]:
    """Retry only when external effect is proven absent (or never started)."""
    state = assignment.external_effect_state or "none_known"
    if state in CONSEQUENTIAL_BLOCK_STATES:
        return False, f"blocked: external_effect_state={state} (UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT)"
    if state not in SAFE_RETRY_EFFECT_STATES:
        return False, f"blocked: unrecognized effect state {state}"
    return True, "safe_retry"


def mark_failure(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    failure_class: str,
    external_effect_state: str,
    partial_result: dict | None = None,
    evidence_refs: list | None = None,
) -> WorkforceAssignmentCheckpoint:
    cp = record_checkpoint(
        db,
        owner_id=owner_id,
        assignment=assignment,
        checkpoint_kind=_kind_for_failure(failure_class),
        partial_result=partial_result,
        evidence_refs=evidence_refs,
        external_effect_state=external_effect_state,
        failure_class=failure_class,
        recoverable=external_effect_state in SAFE_RETRY_EFFECT_STATES,
    )
    assignment.status = "failed"
    assignment.updated_at = datetime.utcnow()
    db.flush()
    return cp


def _kind_for_failure(failure_class: str) -> str:
    mapping = {
        "agent_crash": "crash",
        "provider_timeout": "timeout",
        "provider_rate_limit": "rate_limit",
        "malformed_output": "malformed",
        "partial_result": "partial_result",
        "lost_lease": "lost_lease",
        "mainai_restart": "restart",
        "worker_restart": "restart",
        "expired_assignment": "crash",
        "revoked_agent": "crash",
        "retired_agent": "crash",
    }
    return mapping.get(failure_class, "crash")


def safe_retry_same_agent(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
) -> WorkforceAssignment:
    """Increment retry_count only when safe. Does not re-invoke providers."""
    ok, reason = can_safely_retry(assignment)
    if not ok:
        raise FailureTakeoverError(reason)
    live = assignment_authority_is_live(assignment)
    if not live.live:
        raise FailureTakeoverError(f"assignment authority not live: {', '.join(live.reasons)}")
    assignment.retry_count = int(assignment.retry_count or 0) + 1
    assignment.status = "assigned"
    assignment.updated_at = datetime.utcnow()
    record_checkpoint(
        db,
        owner_id=owner_id,
        assignment=assignment,
        checkpoint_kind="resume",
        partial_result=(latest_checkpoint(db, owner_id=owner_id, assignment_id=assignment.id) or type("X", (), {"partial_result": {}})()).partial_result,
        external_effect_state="none_proven",
        provenance={"retry": True, "same_agent": True},
    )
    db.flush()
    return assignment


def alternate_agent_takeover(
    db: Session,
    *,
    owner_id: uuid.UUID,
    failed_assignment: WorkforceAssignment,
    request: WorkforceDelegationRequest,
    alternate_profile_id: uuid.UUID,
    verifier_profile_id: uuid.UUID | None = None,
    context_items: list[dict] | None = None,
) -> WorkforceAssignment:
    """Reassign to another agent. Authority is re-minted minimal — never widened from failed."""
    from app.workforce.kill_switch import assert_authority_grant_allowed

    grant_fence = assert_authority_grant_allowed(db, owner_id=owner_id)

    ok, reason = can_safely_retry(failed_assignment)
    if not ok:
        raise FailureTakeoverError(reason)

    alt = get_workforce_agent(db, owner_id=owner_id, agent_id=alternate_profile_id)
    assert_agent_selectable(alt)
    if alternate_profile_id == failed_assignment.profile_id:
        raise FailureTakeoverError("alternate agent must differ from failed assignment agent")

    # Preserve partial evidence via checkpoint on the failed assignment.
    prior = latest_checkpoint(db, owner_id=owner_id, assignment_id=failed_assignment.id)
    preserved = dict(prior.partial_result) if prior else dict(failed_assignment.result_payload or {})

    # Narrow authority: intersection-style — only tools the alternate already has, writes empty
    # unless failed also had writes (still no widening beyond alternate's allowed_tool_classes).
    prior_tools = set(failed_assignment.allowed_tool_classes or [])
    alt_tools = set(alt.allowed_tool_classes or [])
    tools = tuple(sorted(prior_tools & alt_tools))
    read_paths = tuple(failed_assignment.allowed_read_paths or ())
    # Never inherit write paths on takeover unless explicitly empty-safe: default no writes.
    authority = TaskScopedAuthority(
        allowed_read_paths=read_paths,
        allowed_write_paths=(),
        allowed_tool_classes=tools,
        allowed_network_destinations=(),
        spend_ceiling_usd=failed_assignment.spend_ceiling_usd,
        allow_execution_effects=False,  # never widen effects on takeover
        expires_at=datetime.utcnow() + timedelta(hours=12),
    )

    # Mark failed superseded without deleting evidence.
    failed_assignment.verification_status = "SUPERSEDED"
    failed_assignment.updated_at = datetime.utcnow()
    revoke_assignment_authority(failed_assignment, reason="takeover_reassign")
    failed_assignment.status = "superseded"  # takeover successor — not merely revoked

    # Re-open request for broker if needed.
    if request.status != "open":
        request.status = "open"
        request.resolved_at = None

    # Force selection of the alternate by registering a one-shot prefer via resolve after
    # temporarily disabling other candidates is heavy — mint assignment directly with same
    # envelope rules as broker, without calling select_best.
    from app.workforce.broker import ingest_untrusted_result  # noqa: F401 — keep import surface
    from app.workforce.performance import record_job_attempt

    package = create_context_package(
        db,
        owner_id=owner_id,
        trust_zone=alt.trust_zone,
        requested_items=list(context_items or []),
        provenance={
            "takeover_of": str(failed_assignment.id),
            "preserved_partial": True,
        },
    )
    if request.verification_requirement == "independent_verifier":
        if verifier_profile_id is None:
            verifier_profile_id = failed_assignment.verifier_profile_id
        if verifier_profile_id is None or verifier_profile_id == alt.id:
            raise FailureTakeoverError("independent verifier required and must differ from builder")

    new_assignment = WorkforceAssignment(
        owner_id=owner_id,
        delegation_request_id=request.id,
        profile_id=alt.id,
        context_package_id=package.id,
        allowed_read_paths=list(authority.allowed_read_paths),
        allowed_write_paths=list(authority.allowed_write_paths),
        allowed_tool_classes=list(authority.allowed_tool_classes),
        allowed_network_destinations=[],
        allowed_project_ids=list(failed_assignment.allowed_project_ids or []),
        spend_ceiling_usd=authority.spend_ceiling_usd,
        allow_execution_effects=False,
        expires_at=authority.expires_at,
        verification_status="UNVERIFIED",
        verifier_profile_id=verifier_profile_id,
        status="assigned",
        result_treated_as_data=True,
        selection_score={"takeover": True, "from_assignment": str(failed_assignment.id)},
        provenance={
            "authority_granted_extra": False,
            "authority_widened": False,
            "takeover": True,
            "authority_fence": grant_fence,
        },
        takeover_of_assignment_id=failed_assignment.id,
        supersedes_assignment_id=failed_assignment.id,
        external_effect_state="none_proven",
        retry_count=0,
        result_payload={"inherited_partial": preserved},
    )
    db.add(new_assignment)
    failed_assignment.supersedes_assignment_id = None  # successor points back via takeover_of
    request.status = "assigned"
    request.resolved_at = datetime.utcnow()
    alt.last_used_at = datetime.utcnow()
    db.flush()

    record_checkpoint(
        db,
        owner_id=owner_id,
        assignment=new_assignment,
        checkpoint_kind="takeover",
        partial_result=preserved,
        external_effect_state="none_proven",
        provenance={"from_assignment": str(failed_assignment.id)},
    )
    record_job_attempt(
        db,
        owner_id=owner_id,
        profile_id=alt.id,
        capability_tag=request.required_capability,
    )
    return new_assignment


def resume_after_restart(
    db: Session,
    *,
    owner_id: uuid.UUID,
    assignment: WorkforceAssignment,
    restart_kind: str = "mainai_restart",
) -> WorkforceAssignmentCheckpoint:
    """Durable resume after MainAI/worker restart — preserves checkpoint, no auto re-invoke."""
    if restart_kind not in ("mainai_restart", "worker_restart"):
        raise FailureTakeoverError(f"invalid restart_kind: {restart_kind}")
    prior = latest_checkpoint(db, owner_id=owner_id, assignment_id=assignment.id)
    return record_checkpoint(
        db,
        owner_id=owner_id,
        assignment=assignment,
        checkpoint_kind="restart",
        partial_result=dict(prior.partial_result) if prior else {},
        evidence_refs=list(prior.evidence_refs) if prior else [],
        external_effect_state=assignment.external_effect_state or "unknown",
        failure_class=restart_kind,
        recoverable=assignment.external_effect_state in SAFE_RETRY_EFFECT_STATES,
        provenance={"resume_after_restart": True},
    )
