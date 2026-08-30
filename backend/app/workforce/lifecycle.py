"""Agent hiring + learning lifecycle (T9/T10).

New agents start lowest trust. Do NOT set trained=True unless actual training happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.workforce import WorkforceAgentProfile
from app.models.workforce_ops import WorkforceLifecycleEvent
from app.workforce.registry import WorkforceRegistryError, get_workforce_agent, register_workforce_agent


class LifecycleError(Exception):
    pass


# Allowed transitions (from -> to). None from = create.
_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"need_detected", "candidate"}),
    "need_detected": frozenset({"candidate"}),
    "candidate": frozenset({"sandbox", "retired", "disabled"}),
    "sandbox": frozenset({"sandbox", "probation", "candidate", "retired", "disabled"}),
    "probation": frozenset({"probation", "active", "sandbox", "retired", "disabled"}),
    "active": frozenset({"active", "disabled", "retired"}),
    "disabled": frozenset({"active", "retired", "probation"}),
    "retired": frozenset(),
}

_IMPROVE_KINDS = frozenset(
    {
        "improve_policy",
        "improve_tools",
        "improve_playbook",
        "improve_retrieval",
        "provider_swap",
        "fine_tune",
    }
)


def _record_event(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile: WorkforceAgentProfile,
    from_status: str | None,
    to_status: str,
    change_kind: str,
    change_summary: str,
    evidence_before: dict | None = None,
    evidence_after: dict | None = None,
    rollback_ref: str | None = None,
    trained: bool = False,
) -> WorkforceLifecycleEvent:
    if trained and change_kind != "fine_tune":
        raise LifecycleError("trained=True only allowed for fine_tune change_kind")
    event = WorkforceLifecycleEvent(
        owner_id=owner_id,
        profile_id=profile.id,
        from_status=from_status,
        to_status=to_status,
        change_kind=change_kind,
        change_summary=change_summary,
        evidence_before=dict(evidence_before or {}),
        evidence_after=dict(evidence_after or {}),
        rollback_ref=rollback_ref,
        trained=trained,
        provenance={"lowest_trust_on_hire": from_status is None},
    )
    db.add(event)
    db.flush()
    return event


def detect_need_and_create_candidate(
    db: Session,
    *,
    owner_id: uuid.UUID,
    agent_key: str,
    name: str,
    role: str,
    agent_type: str,
    capability_tags: list[str],
    need_summary: str,
    trust_zone: str = "UNTRUSTED_REMOTE",
) -> tuple[WorkforceAgentProfile, WorkforceLifecycleEvent]:
    """NEED DETECTED → candidate. Creating grants ZERO authority; status=candidate."""
    profile = register_workforce_agent(
        db,
        owner_id=owner_id,
        agent_key=agent_key,
        name=name,
        role=role,
        agent_type=agent_type,
        capability_tags=capability_tags,
        trust_zone=trust_zone,
        status="candidate",
        allowed_tool_classes=(),
        provenance={"need_summary": need_summary, "authority_on_create": False},
    )
    event = _record_event(
        db,
        owner_id=owner_id,
        profile=profile,
        from_status=None,
        to_status="candidate",
        change_kind="candidate_created",
        change_summary=need_summary,
        evidence_before={},
        evidence_after={"status": "candidate", "capabilities": list(capability_tags)},
    )
    return profile, event


def advance_lifecycle(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile_id: uuid.UUID,
    to_status: str,
    change_kind: str,
    change_summary: str,
    evidence_before: dict | None = None,
    evidence_after: dict | None = None,
    rollback_ref: str | None = None,
    trained: bool = False,
) -> tuple[WorkforceAgentProfile, WorkforceLifecycleEvent]:
    profile = get_workforce_agent(db, owner_id=owner_id, agent_id=profile_id)
    allowed = _TRANSITIONS.get(profile.status, frozenset())
    if to_status not in allowed and not (profile.status == "active" and to_status == "active"):
        raise LifecycleError(f"illegal transition {profile.status} → {to_status}")
    if change_kind in _IMPROVE_KINDS and profile.status not in ("active", "probation", "sandbox"):
        raise LifecycleError("improvements only from sandbox/probation/active")

    before = profile.status
    profile.status = to_status
    if to_status == "retired":
        profile.retired_at = datetime.utcnow()
    profile.updated_at = datetime.utcnow()
    event = _record_event(
        db,
        owner_id=owner_id,
        profile=profile,
        from_status=before,
        to_status=to_status,
        change_kind=change_kind,
        change_summary=change_summary,
        evidence_before=evidence_before or {"status": before},
        evidence_after=evidence_after or {"status": to_status},
        rollback_ref=rollback_ref,
        trained=trained,
    )
    db.flush()
    return profile, event


def record_improvement(
    db: Session,
    *,
    owner_id: uuid.UUID,
    profile_id: uuid.UUID,
    change_kind: str,
    change_summary: str,
    evidence_before: dict,
    evidence_after: dict,
    rollback_ref: str,
    trained: bool = False,
) -> WorkforceLifecycleEvent:
    """Policy/prompt/tools/playbook/retrieval/provider swap — or fine_tune if trained=True."""
    if change_kind not in _IMPROVE_KINDS:
        raise LifecycleError(f"not an improvement kind: {change_kind}")
    if trained and change_kind != "fine_tune":
        raise LifecycleError("Do NOT call this trained unless actual training happened")
    profile = get_workforce_agent(db, owner_id=owner_id, agent_id=profile_id)
    _, event = advance_lifecycle(
        db,
        owner_id=owner_id,
        profile_id=profile_id,
        to_status=profile.status,
        change_kind=change_kind,
        change_summary=change_summary,
        evidence_before=evidence_before,
        evidence_after=evidence_after,
        rollback_ref=rollback_ref,
        trained=trained,
    )
    return event


def run_hiring_pipeline(
    db: Session,
    *,
    owner_id: uuid.UUID,
    agent_key: str,
    name: str,
    role: str,
    agent_type: str,
    capability_tags: list[str],
    need_summary: str,
    benchmark_evidence: dict,
    adversarial_evidence: dict,
    stop_at: str = "probation",
) -> WorkforceAgentProfile:
    """need → candidate → sandbox → benchmark → adversarial → probation|active.

    Default stop_at=probation (lowest trust still elevated). Never auto-grants authority.
    """
    profile, _ = detect_need_and_create_candidate(
        db,
        owner_id=owner_id,
        agent_key=agent_key,
        name=name,
        role=role,
        agent_type=agent_type,
        capability_tags=capability_tags,
        need_summary=need_summary,
    )
    advance_lifecycle(
        db,
        owner_id=owner_id,
        profile_id=profile.id,
        to_status="sandbox",
        change_kind="enter_sandbox",
        change_summary="enter sandbox",
    )
    advance_lifecycle(
        db,
        owner_id=owner_id,
        profile_id=profile.id,
        to_status="sandbox",
        change_kind="benchmark",
        change_summary="benchmark tasks",
        evidence_after=benchmark_evidence,
    )
    advance_lifecycle(
        db,
        owner_id=owner_id,
        profile_id=profile.id,
        to_status="sandbox",
        change_kind="adversarial_test",
        change_summary="adversarial tests",
        evidence_after=adversarial_evidence,
    )
    if stop_at == "sandbox":
        return profile
    advance_lifecycle(
        db,
        owner_id=owner_id,
        profile_id=profile.id,
        to_status="probation",
        change_kind="enter_probation",
        change_summary="limited probation",
    )
    if stop_at == "probation":
        return profile
    if stop_at == "active":
        advance_lifecycle(
            db,
            owner_id=owner_id,
            profile_id=profile.id,
            to_status="active",
            change_kind="activate",
            change_summary="activate after probation evidence",
            evidence_before=benchmark_evidence,
            evidence_after=adversarial_evidence,
        )
    return profile
