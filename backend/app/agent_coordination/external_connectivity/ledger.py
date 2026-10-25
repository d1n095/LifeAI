"""Map independently checkable provider evidence onto the existing #302/#303 ledger.

This is not a second inbox. Verified Cursor webhook or run-status evidence would
reuse DurableCoordinationService + delivery state transitions. This module only
*proposes* an event. It never persists, never self-asserts independently_verified,
and never claims vendor delivery for a fixture.
"""

from __future__ import annotations

from typing import Any

from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.external_connectivity.identity import IdentityClass

STATUS_TO_DELIVERY = {
    "FINISHED": "completed",
    "ERROR": "failed",
}


def propose_ledger_event(verified: dict[str, Any]) -> dict[str, Any]:
    """Return the event the existing gateway would record. Does not write the ledger."""
    if verified.get("outcome") != "accepted":
        return {
            "persisted": False,
            "proposed": False,
            "reason": "Unverified provider evidence must not enter the durable ledger as delivery.",
            "vendor_delivery_claimed": False,
            "independently_verified": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }
    payload = verified.get("payload") or {}
    provider_status = str(verified.get("status") or payload.get("status") or "")
    proposed_status = STATUS_TO_DELIVERY.get(provider_status)
    target = payload.get("target") or {}
    fixture = bool(verified.get("fixture"))
    provider_authenticated = bool(verified.get("provider_authenticated")) and not fixture
    return {
        "persisted": False,
        "proposed": True,
        "reuse": {
            "gateway": "app.agent_coordination.delivery",
            "ledger": "app.agent_coordination.durable.DurableCoordinationService",
            "persist": "app.agent_coordination.persist.persist_owner_ledger",
            "agent_side": "Founder-issued local runner remains the local path; this event is provider-side.",
        },
        "assignment_receipt": {
            "how": "POST /v1/agents response agent.id + initial run.id, later bound to an existing AgentWorkAssignment.",
            "independently_checkable": "GET /v1/agents/{id} with the same provider API key.",
        },
        "authorized_execution": {
            "how": "Provider run exists under the Founder/service-account key, not under LIFE_AGENT_BRIDGE_TOKEN.",
            "independently_checkable": "GET /v1/agents/{id}/runs/{runId} status in CREATING/RUNNING.",
        },
        "completion_evidence": {
            "how": "v0 HMAC statusChange FINISHED/ERROR and/or GET run in a terminal state.",
            "independently_checkable": True,
            "provider_status": provider_status,
            "proposed_delivery_status": proposed_status,
            "agent_id": verified.get("agent_id") or payload.get("id"),
            "summary_present": bool(payload.get("summary")),
            "branch_present": bool(target.get("branchName")),
            "pr_url_present": bool(target.get("prUrl")),
        },
        "agent_identity": {
            "identity_class": verified.get("identity_class") or IdentityClass.cursor_cloud_webhook_hmac.value,
            "provider_authenticated": provider_authenticated,
            "local_bridge_token": False,
            "this_cloud_agent_runtime": False,
            "distinct_from_examiner": True,
        },
        "event": {
            "kind": "provider_status_change",
            "event_id": f"cursor-cloud-{payload.get('id') or 'unknown'}-{provider_status or 'unknown'}",
            "status": proposed_status or "blocked",
        },
        "fixture": fixture,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "note": (
            "A fixture HMAC is reproducible cryptography, not Cursor delivery. "
            "Independent verification still requires an examiner identity distinct from the builder."
        ),
    }
