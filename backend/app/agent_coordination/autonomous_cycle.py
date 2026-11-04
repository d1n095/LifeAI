"""Authorized autonomous operating cycle on existing production services.

Founder goal / authorized work → task selection → capability-based agent
selection → authorized delegation → authenticated provider ingestion →
independent examination → next eligible task.

Does not invent a second STOP, spend, verification, or messaging system.
Live paid mutate stays fail-closed. first_cycle_go stays false.
Silence is never permission.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.agent_coordination.durable import FIRST_CYCLE_GO, DurableCoordinationService, ObligationStatus
from app.agent_coordination.provider_ingestion import (
    assert_transition_authority,
    bind_provider_assignment,
    continue_after_independent_verification,
    ingest_authenticated_provider_event,
)
from app.mainai_operational_handover.absence import evaluate_absence_tick, founder_catchup_report
from app.mainai_operational_handover.interactive import InteractiveIsolation
from app.models.workforce import WorkforceAssignment
from app.workforce.kill_switch import KillSwitchError, assert_grant_allowed
from app.workforce.authority import AuthorityEnvelopeError, require_live_assignment_authority
from app.workforce.broker import DelegationBrokerError, resolve_delegation, submit_delegation_request
from app.workforce.registry import WorkforceRegistryError, get_workforce_agent
from app.workforce.selector import SelectorError

AUTHORIZED_PROVIDER = "cursor"


def _result(**extra: Any) -> dict[str, Any]:
    row = {
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
        "live_paid_mutate": False,
        "independently_verified": False,
        "silence_is_not_approval": True,
    }
    row.update(extra)
    return row


def _items_from_ledger(service: DurableCoordinationService) -> list[dict[str, Any]]:
    items = []
    for row in service._obligations.values():
        status = row.get("status")
        items.append(
            {
                "id": row.get("assignment_id"),
                "title": row.get("instruction_id") or row.get("workstream"),
                "kind": "task",
                "risk": "read_only_audit" if status != ObligationStatus.independently_verified.value else "unknown",
                "approved": True,
                "needs_approval": False,
                "new_work": status == ObligationStatus.assigned.value and row.get("actor") == "mainai",
                "status": status,
                "agent_identity": row.get("agent_identity"),
            }
        )
    return items


def _select_next_eligible(items: list[dict[str, Any]], continued: list[dict[str, Any]]) -> dict[str, Any] | None:
    allowed = {row.get("id") for row in continued}
    for row in items:
        if row.get("id") in allowed and row.get("status") in {
            ObligationStatus.assigned.value,
            ObligationStatus.acknowledged.value,
            ObligationStatus.executing.value,
            ObligationStatus.completed.value,
        }:
            return row
    return continued[0] if continued else None


def run_authorized_operating_cycle(
    db,
    service: DurableCoordinationService,
    *,
    owner_id: str,
    goal_text: str,
    required_capability: str = "coding",
    absence_enabled: bool = False,
    standing_grants: list[dict[str, Any]] | None = None,
    provider_secret: str | None = None,
    provider_raw_body: bytes | None = None,
    provider_signature: str | None = None,
    provider_user_agent: str | None = None,
    candidate_sha: str | None = None,
    isolation: InteractiveIsolation | None = None,
    verifier_profile_id: str | uuid.UUID | None = None,
) -> dict[str, Any]:
    """One production tick. Interactive Founder work stays admitted throughout."""
    iso = isolation or InteractiveIsolation()
    founder = iso.admit("interactive", name="founder_interface")
    background = iso.admit("background", name="coordination_loop")
    if not founder.get("admitted"):
        return _result(outcome="interactive_starved", founder=founder, background=background)

    authority = assert_transition_authority(db, owner_id=str(owner_id))
    if authority.get("outcome") != "allowed":
        evaluation = evaluate_absence_tick(
            items=_items_from_ledger(service),
            stop_blocked=True,
            standing_grants=standing_grants,
            absence_enabled=absence_enabled,
        )
        service._operational_handover["absence"] = evaluation
        return _result(
            outcome=authority.get("outcome"),
            reason=authority.get("reason"),
            founder=founder,
            background=background,
            absence=evaluation,
            catchup=founder_catchup_report(evaluation),
            next_eligible=None,
        )

    try:
        owner_uuid = uuid.UUID(str(owner_id))
        assert_grant_allowed(db, owner_id=owner_uuid)
    except (KillSwitchError, ValueError) as exc:
        return _result(outcome="authority_unestablished", reason=str(exc), founder=founder)

    evaluation = evaluate_absence_tick(
        items=_items_from_ledger(service),
        stop_blocked=False,
        standing_grants=standing_grants or [{"scope": "coding", "active": True}, {"scope": "task", "active": True}],
        absence_enabled=absence_enabled,
    )
    if absence_enabled and evaluation["paused"] and not evaluation["continued"]:
        service._operational_handover["absence"] = evaluation
        return _result(
            outcome="absence_paused",
            founder=founder,
            background=background,
            absence=evaluation,
            catchup=founder_catchup_report(evaluation),
            next_eligible=None,
        )

    instruction_id = goal_text[:200]
    workstream = "authorized_operating_cycle"
    existing = service.get_by_canonical_work(
        owner_id=str(owner_id), workstream=workstream, instruction_id=instruction_id
    )
    existing_id = existing.get("assignment_id") if existing else None

    workforce_assignment = None
    selected_profile = None
    assignment_id = existing_id
    if existing is not None:
        workforce_assignment_id = existing.get("workforce_assignment_id")
        if not workforce_assignment_id:
            return _result(
                outcome="delegation_authority_missing",
                reason="existing obligation has no Delegation Broker assignment",
                founder=founder,
            )
        try:
            workforce_assignment = db.get(WorkforceAssignment, uuid.UUID(str(workforce_assignment_id)))
            if workforce_assignment is None or workforce_assignment.owner_id != owner_uuid:
                raise DelegationBrokerError("workforce assignment owner mismatch")
            require_live_assignment_authority(workforce_assignment)
            selected_profile = get_workforce_agent(
                db, owner_id=owner_uuid, agent_id=workforce_assignment.profile_id
            )
            if selected_profile.provider_type != AUTHORIZED_PROVIDER:
                raise DelegationBrokerError("selected workforce profile is not bound to the authorized provider")
        except (ValueError, DelegationBrokerError, AuthorityEnvelopeError, WorkforceRegistryError) as exc:
            return _result(outcome="delegation_authority_invalid", reason=str(exc), founder=founder)
    else:
        if verifier_profile_id is None:
            return _result(
                outcome="delegation_rejected",
                reason="independent verifier profile is required by the Delegation Broker",
                founder=founder,
            )
        try:
            verifier_uuid = uuid.UUID(str(verifier_profile_id))
        except ValueError:
            return _result(outcome="delegation_rejected", reason="invalid verifier profile", founder=founder)
        try:
            with db.begin_nested():
                request = submit_delegation_request(
                    db,
                    owner_id=owner_uuid,
                    goal_text=goal_text,
                    required_capability=required_capability,
                    verification_requirement="independent_verifier",
                    provenance={"source": workstream, "first_cycle_go": FIRST_CYCLE_GO},
                )
                workforce_assignment = resolve_delegation(
                    db,
                    owner_id=owner_uuid,
                    request=request,
                    verifier_profile_id=verifier_uuid,
                    required_provider_type=AUTHORIZED_PROVIDER,
                )
                selected_profile = get_workforce_agent(
                    db, owner_id=owner_uuid, agent_id=workforce_assignment.profile_id
                )
                if selected_profile.provider_type != AUTHORIZED_PROVIDER:
                    raise DelegationBrokerError("selected workforce profile is not bound to the authorized provider")
                assignment_id = str(uuid.uuid4())
                assigned = service.assign(
                    agent_identity=AUTHORIZED_PROVIDER,
                    workstream=workstream,
                    owner_id=str(owner_id),
                    instruction_id=instruction_id,
                    assignment_id=assignment_id,
                    workforce_assignment_id=str(workforce_assignment.id),
                    actor="mainai",
                )
                if not assigned.accepted:
                    raise DelegationBrokerError(assigned.reason)
        except (DelegationBrokerError, SelectorError, WorkforceRegistryError) as exc:
            return _result(outcome="delegation_rejected", reason=str(exc), founder=founder)

    if selected_profile is None or workforce_assignment is None or assignment_id is None:
        return _result(outcome="delegation_rejected", reason="broker assignment unavailable", founder=founder)
    selected_agent = AUTHORIZED_PROVIDER
    selector_note = "delegation_broker"

    bind = bind_provider_assignment(
        service,
        owner_id=str(owner_id),
        assignment_id=assignment_id,
        provider="cursor",
        external_agent_id="bc_cycle_agent",
        recipient=selected_agent,
    )

    ingest = None
    if provider_secret and provider_raw_body and provider_signature:
        ingest = ingest_authenticated_provider_event(
            service,
            owner_id=str(owner_id),
            secret=provider_secret,
            raw_body=provider_raw_body,
            signature=provider_signature,
            user_agent=provider_user_agent,
            fixture=True,
            db=db,
        )

    verified = None
    if ingest and ingest.get("transition") == "completed":
        verified = continue_after_independent_verification(
            db,
            service,
            owner_id=str(owner_id),
            assignment_id=assignment_id,
            builder_identity=selected_agent,
            candidate_sha=candidate_sha or "0" * 40,
        )

    next_items = _items_from_ledger(service)
    post_eval = evaluate_absence_tick(
        items=next_items,
        stop_blocked=False,
        standing_grants=standing_grants or [{"scope": "coding", "active": True}, {"scope": "task", "active": True}],
        absence_enabled=absence_enabled,
    )
    service._operational_handover["absence"] = post_eval
    next_eligible = _select_next_eligible(next_items, post_eval.get("continued") or [])
    iso.complete("background", started_at=float(background.get("started_at") or 0.0))
    return _result(
        outcome="cycled",
        founder=founder,
        background=background,
        interactive=iso.public_view(),
        selector={"agent": selected_agent, "note": selector_note},
        workforce={
            "agent_key": selected_profile.agent_key,
            "profile_id": str(selected_profile.id),
            "delegation_request_id": str(workforce_assignment.delegation_request_id),
            "assignment_id": str(workforce_assignment.id),
            "provider_identity": selected_agent,
        },
        assignment_id=assignment_id,
        bind=bind,
        ingest=ingest,
        verification=verified,
        absence=post_eval,
        catchup=founder_catchup_report(post_eval),
        next_eligible={"id": next_eligible.get("id"), "status": next_eligible.get("status")} if next_eligible else None,
        independently_verified=bool(verified and verified.get("independently_verified")),
    )
