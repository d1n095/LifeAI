"""Authenticated provider event → binding check → durable ingest → trusted transition.

Reuses the #302/#306 gateway and DurableCoordinationService. Webhook completion
is never independent certification. Live paid mutate stays fail-closed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent_coordination.delivery import FIRST_CYCLE_GO, ensure_delivery
from app.agent_coordination.durable import DurableCoordinationService, ObligationStatus
from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
from app.agent_coordination.external_connectivity.identity import IdentityClass

PROVIDER_CURSOR = "cursor"
MAX_EVENT_AGE = timedelta(hours=24)
TERMINAL_SUCCESS = {"FINISHED"}
TERMINAL_FAILURE = {"ERROR", "FAILED"}
NON_COMPLETION = {"UNKNOWN", "BLOCKED", "CREATING", "RUNNING", "EXPIRED"}


def _iso(when: datetime | None = None) -> str:
    stamp = when or datetime.now(timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _result(*, outcome: str, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "outcome": outcome,
        "reason": reason,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "live_paid_mutate": False,
    }
    if extra:
        row.update(extra)
    return row


def bind_provider_assignment(
    service: DurableCoordinationService,
    *,
    owner_id: str,
    assignment_id: str,
    provider: str,
    external_agent_id: str,
    recipient: str = "cursor",
) -> dict[str, Any]:
    """Bind provider + external agent + assignment + owner. Does not create a vendor agent."""
    obligation = service.get(assignment_id)
    if obligation is None:
        return _result(outcome="rejected", reason="assignment not found")
    if str(obligation.get("owner_id")) != str(owner_id):
        return _result(outcome="forged_rejected", reason="cross-owner binding rejected")
    if obligation.get("agent_identity") != recipient:
        return _result(outcome="rejected", reason="assignment recipient does not match bind recipient")
    binding = {
        "owner_id": str(owner_id),
        "assignment_id": assignment_id,
        "provider": provider,
        "external_agent_id": external_agent_id,
        "recipient": recipient,
        "bound_at": _iso(),
    }
    service._provider_ingestion["bindings"][assignment_id] = binding
    return {
        "outcome": "bound",
        "binding": binding,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def _binding_for_agent(service: DurableCoordinationService, external_agent_id: str) -> dict[str, Any] | None:
    for row in (service._provider_ingestion.get("bindings") or {}).values():
        if row.get("external_agent_id") == external_agent_id:
            return row
    return None


def ingest_authenticated_provider_event(
    service: DurableCoordinationService,
    *,
    owner_id: str,
    secret: str,
    raw_body: bytes,
    signature: str,
    user_agent: str | None = None,
    fixture: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify provider evidence, then apply one trusted lifecycle transition."""
    ensure_delivery(service)
    verified = verify_v0_webhook(
        secret=secret,
        raw_body=raw_body,
        signature=signature,
        user_agent=user_agent,
        fixture=fixture,
    )
    if verified.get("outcome") != "accepted":
        return _result(
            outcome="forged_rejected",
            reason="provider signature rejected",
            extra={"identity_class": IdentityClass.cursor_cloud_webhook_hmac.value},
        )
    payload = verified.get("payload") or {}
    agent_id = str(verified.get("agent_id") or payload.get("id") or "")
    status = str(verified.get("status") or payload.get("status") or "UNKNOWN").upper()
    occurred = _parse_iso(str(payload.get("timestamp") or "")) or now or datetime.now(timezone.utc)
    event_id = f"{PROVIDER_CURSOR}:{agent_id}:{status}:{payload.get('timestamp') or _iso(occurred)}"
    binding = _binding_for_agent(service, agent_id)
    if binding is None:
        return _result(outcome="rejected", reason="unbound external agent")
    if str(binding.get("owner_id")) != str(owner_id):
        return _result(outcome="forged_rejected", reason="cross-owner event rejected")
    processed = service._provider_ingestion.setdefault("processed_event_ids", [])
    if event_id in processed:
        return _result(outcome="replay_ignored", reason="duplicate provider event_id")
    clock = now or datetime.now(timezone.utc)
    if occurred < clock - MAX_EVENT_AGE or occurred > clock + timedelta(minutes=5):
        return _result(outcome="expired_rejected", reason="provider event expired or implausible")
    last_raw = (service._provider_ingestion.get("last_timestamp") or {}).get(agent_id)
    last = _parse_iso(last_raw)
    if last is not None and occurred < last:
        return _result(outcome="reordered_rejected", reason="provider event older than last accepted event")

    assignment_id = binding["assignment_id"]
    recipient = binding["recipient"]
    if status in TERMINAL_SUCCESS:
        occurred_iso = _iso(occurred)
        service.acknowledge(
            assignment_id=assignment_id,
            agent_identity=recipient,
            event_id=f"provider-ack:{event_id}",
            occurred_at=occurred_iso,
        )
        service.record_execution_progress(
            assignment_id=assignment_id,
            agent_identity=recipient,
            evidence_kind="adapter_heartbeat",
            event_id=f"provider-exec:{event_id}",
            occurred_at=occurred_iso,
            evidence={
                "source": "provider_webhook",
                "provider": PROVIDER_CURSOR,
                "external_agent_id": agent_id,
                "provider_status": status,
                "fixture": fixture,
            },
        )
        applied = service.complete(
            assignment_id=assignment_id,
            agent_identity=recipient,
            event_id=f"provider-complete:{event_id}",
            occurred_at=occurred_iso,
            evidence={
                "source": "provider_webhook",
                "provider": PROVIDER_CURSOR,
                "external_agent_id": agent_id,
                "provider_status": status,
                "fixture": fixture,
                "execution_evidence": True,
            },
        )
        transition = "completed"
        _prepare_examiner(service, owner_id=owner_id, parent_assignment_id=assignment_id)
    elif status in TERMINAL_FAILURE:
        applied = service.fail(
            assignment_id=assignment_id,
            agent_identity=recipient,
            event_id=f"provider-fail:{event_id}",
            occurred_at=_iso(occurred),
            reason=f"provider_{status.lower()}",
        )
        transition = "failed"
    else:
        applied = service.block(
            assignment_id=assignment_id,
            agent_identity=recipient,
            reason=f"non_completion_status_{status.lower()}",
            missing_capability="provider_terminal_evidence",
            event_id=f"provider-block:{event_id}",
            occurred_at=_iso(occurred),
        )
        transition = "blocked"
        if status in {"UNKNOWN", "BLOCKED"}:
            return _after_store(
                service,
                event_id=event_id,
                agent_id=agent_id,
                occurred=occurred,
                payload=payload,
                binding=binding,
                applied=applied,
                transition=transition,
                extra={"note": "UNKNOWN/BLOCKED is not verified completion"},
            )

    return _after_store(
        service,
        event_id=event_id,
        agent_id=agent_id,
        occurred=occurred,
        payload=payload,
        binding=binding,
        applied=applied,
        transition=transition,
        extra={"fixture": fixture, "provider_authenticated": bool(verified.get("provider_authenticated"))},
    )


def _after_store(
    service: DurableCoordinationService,
    *,
    event_id: str,
    agent_id: str,
    occurred: datetime,
    payload: dict[str, Any],
    binding: dict[str, Any],
    applied: Any,
    transition: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    service._provider_ingestion.setdefault("processed_event_ids", []).append(event_id)
    service._provider_ingestion.setdefault("last_timestamp", {})[agent_id] = _iso(occurred)
    service._provider_ingestion.setdefault("events", []).append(
        {
            "event_id": event_id,
            "external_agent_id": agent_id,
            "assignment_id": binding["assignment_id"],
            "owner_id": binding["owner_id"],
            "provider": binding["provider"],
            "transition": transition,
            "provider_status": payload.get("status"),
            "independently_verified": False,
        }
    )
    accepted = bool(getattr(applied, "accepted", True))
    return _result(
        outcome="applied" if accepted else str(getattr(applied, "outcome", "rejected")),
        reason=str(getattr(applied, "reason", transition)),
        extra={
            "transition": transition,
            "assignment_id": binding["assignment_id"],
            "external_agent_id": agent_id,
            "provider": binding["provider"],
            "owner_id": binding["owner_id"],
            "event_id": event_id,
            "independently_verified": False,
            **(extra or {}),
        },
    )


def _prepare_examiner(service: DurableCoordinationService, *, owner_id: str, parent_assignment_id: str) -> None:
    service.assign(
        agent_identity="examiner",
        workstream="independent_review",
        owner_id=owner_id,
        instruction_id=f"review:{parent_assignment_id}",
        assignment_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"examiner:{parent_assignment_id}")),
        actor="mainai",
        event_id=f"examiner-prep:{parent_assignment_id}",
    )


def persist_ingestion(db, *, owner_id, service: DurableCoordinationService) -> None:
    from app.agent_coordination.persist import persist_owner_ledger

    persist_owner_ledger(db, owner_id=owner_id, service=service, actor="provider_ingestion")
