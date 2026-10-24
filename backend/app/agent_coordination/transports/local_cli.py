"""Local CLI transport — execution only. Not vendor messaging or a private session."""

from __future__ import annotations

from typing import Any

from app.agent_coordination.adapter_config import SUPPORTED_LOCAL_CLI_PROVIDERS, adapter_availability, real_adapter_config
from app.agent_coordination.transport import TransportAvailability, TransportCapabilities, fail_closed
from app.agent_coordination.transports import LOCAL_CLI_ID


class LocalCliTransport:
    def transport_id(self) -> str:
        return LOCAL_CLI_ID

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(execute=True, write_scope="founder_allowlisted")

    def availability(self) -> TransportAvailability:
        found = any(adapter_availability(key).executable_found for key in SUPPORTED_LOCAL_CLI_PROVIDERS)
        authorized = any(
            adapter_availability(key).enabled
            and adapter_availability(key).credentials_state == "configured"
            and real_adapter_config(key) is not None
            for key in SUPPORTED_LOCAL_CLI_PROVIDERS
        )
        return TransportAvailability(
            transport_id=LOCAL_CLI_ID,
            surface="local_cli",
            installed=found,
            configured=authorized,
            founder_authorized=authorized,
            reachable=authorized,
            missing_capability=None if authorized else "founder_adapter_enablement",
            reason="local CLI is execution only; it is not a vendor session",
        )

    def bind_identity(self) -> dict[str, Any]:
        return {"transport_id": LOCAL_CLI_ID, "vendor_session": False}

    def deliver(self) -> dict[str, Any]:
        return fail_closed(transport_id=LOCAL_CLI_ID, missing_capability="agent_messaging_adapter", reason="local CLI cannot deliver messages")

    def acknowledge(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return fail_closed(transport_id=LOCAL_CLI_ID, missing_capability="agent_messaging_adapter", reason="local CLI cannot acknowledge")

    def execute(self, message: dict[str, Any]) -> dict[str, Any]:
        avail = self.availability()
        if not avail.founder_authorized:
            return fail_closed(transport_id=LOCAL_CLI_ID, missing_capability=avail.missing_capability or "founder_adapter_enablement", reason=avail.reason)
        del message
        return {"ran": False, "reason": "invoke through RestrictedAssignmentExecutor allowlisted mode", "vendor_delivery_claimed": False}

    def cancel(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return fail_closed(transport_id=LOCAL_CLI_ID, missing_capability="agent_messaging_adapter", reason="local CLI cancel is unused")

    def ingest_result(self, message_id: str, *, event_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return fail_closed(transport_id=LOCAL_CLI_ID, missing_capability="agent_messaging_adapter", reason="results go through the gateway runner")
