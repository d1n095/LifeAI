"""Founder-authorized agent-side runner over the existing delivery gateway.

This is the first verified messaging path. It claims, acknowledges, optionally
executes under least privilege, and returns receipts. It never interpolates
message content into a shell and never claims a vendor private session.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.local_bridge import LocalAuthorizedBridge
from app.agent_coordination.transport import (
    MISSING_RUNNER_AUTH,
    TransportAvailability,
    TransportCapabilities,
    fail_closed,
)
from app.agent_coordination.transports import GATEWAY_RUNNER_ID
from app.agent_coordination.transports.executor import RestrictedAssignmentExecutor


class AuthorizedGatewayTransport:
    def __init__(
        self,
        *,
        token: str,
        request: Callable[..., Any] | None = None,
        executor: RestrictedAssignmentExecutor | None = None,
    ) -> None:
        if not token:
            raise ValueError("Founder-issued bridge token is required")
        self._token = token
        self._bridge = LocalAuthorizedBridge(token=token, request=request)
        self._executor = executor or RestrictedAssignmentExecutor()
        self._seen: set[str] = set()

    def transport_id(self) -> str:
        return GATEWAY_RUNNER_ID

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            deliver=True,
            acknowledge=True,
            execute=True,
            cancel=True,
            ingest_result=True,
            write_scope="none",
        )

    def availability(self) -> TransportAvailability:
        authorized = bool(self._token)
        return TransportAvailability(
            transport_id=GATEWAY_RUNNER_ID,
            surface="agent_side_runner",
            installed=True,
            configured=authorized,
            founder_authorized=authorized,
            reachable=authorized,
            missing_capability=None if authorized else MISSING_RUNNER_AUTH,
            reason="Founder-issued gateway token bound to one recipient" if authorized else "token missing",
        )

    def bind_identity(self) -> dict[str, Any]:
        return {"transport_id": GATEWAY_RUNNER_ID, "token_present": bool(self._token), "vendor_session": False}

    def deliver(self) -> dict[str, Any]:
        avail = self.availability()
        if not avail.founder_authorized:
            return fail_closed(transport_id=GATEWAY_RUNNER_ID, missing_capability=MISSING_RUNNER_AUTH, reason=avail.reason)
        delivered = self._bridge.pull()
        return {
            "outcome": "delivered" if delivered else "empty",
            "transport_id": GATEWAY_RUNNER_ID,
            "delivered_count": len(delivered),
            "messages": delivered,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }

    def acknowledge(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return self._bridge.acknowledge(message_id, event_id=event_id)

    def execute(self, message: dict[str, Any]) -> dict[str, Any]:
        if not self.availability().founder_authorized:
            return fail_closed(transport_id=GATEWAY_RUNNER_ID, missing_capability=MISSING_RUNNER_AUTH, reason="not authorized")
        if not self.capabilities().execute:
            return fail_closed(transport_id=GATEWAY_RUNNER_ID, missing_capability="execution_not_supported", reason="execute disabled")
        message_id = str((message.get("message") or message).get("message_id") or "")
        if message_id in self._seen:
            return {"outcome": "replay_ignored", "reason": "duplicate execution", "vendor_delivery_claimed": False}
        self._seen.add(message_id)
        return self._executor.run(message)

    def cancel(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return self._bridge.submit_receipt(
            {
                "kind": "blocked",
                "event_id": event_id,
                "message_id": message_id,
                "missing_capability": "cancelled_by_agent_side",
            }
        )

    def ingest_result(self, message_id: str, *, event_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("ran"):
            self._bridge.report_execution(message_id, event_id=f"exec-{event_id}", evidence=result)
        return self._bridge.complete(message_id, event_id=event_id, evidence=result)

    def receive_cycle(self, *, execute: bool = True) -> dict[str, Any]:
        """Assignment → claim → ack → optional restricted execute → completion receipt."""
        pulled = self.deliver()
        if pulled.get("outcome") == "blocked":
            return pulled
        receipts = []
        for row in pulled.get("messages") or []:
            message = row.get("message") or row
            message_id = str(message.get("message_id") or "")
            ack = self.acknowledge(message_id, event_id=f"agent-side-ack-{message_id[:12]}")
            executed = {"ran": False, "mode": "skipped"}
            if execute:
                executed = self.execute(row)
                if executed.get("outcome") == "blocked":
                    self.cancel(message_id, event_id=f"agent-side-block-{message_id[:12]}")
                    receipts.append({"delivered": row, "ack": ack, "execute": executed, "complete": executed})
                    continue
            complete = self.ingest_result(message_id, event_id=f"agent-side-done-{message_id[:12]}", result=executed)
            receipts.append({"delivered": row, "ack": ack, "execute": executed, "complete": complete})
        return {
            "outcome": "applied",
            "transport_id": GATEWAY_RUNNER_ID,
            "delivered_count": pulled.get("delivered_count", 0),
            "receipts": receipts,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
            "executor": os.environ.get("LIFE_AGENT_SIDE_EXECUTOR", "record"),
        }

    def close(self) -> None:
        self._bridge.close()
