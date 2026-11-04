"""Blocked vendor / model / remote / private-session transports.

These adapters exist so inventory is complete. They never simulate delivery.
"""

from __future__ import annotations

from typing import Any

from app.agent_coordination.transport import TransportAvailability, TransportCapabilities, fail_closed
from app.agent_coordination.transports import blocked_availability


class BlockedVendorTransport:
    def __init__(
        self,
        *,
        transport_id: str,
        surface: str,
        missing: str,
        bound_identity: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self._id = transport_id
        self._surface = surface
        self._missing = missing
        self._identity = bound_identity

    def transport_id(self) -> str:
        return self._id

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            vendor_session=self._surface == "private_vendor_session",
            model_api=self._surface == "provider_model_api",
            remote_background=self._surface == "remote_background_agent",
        )

    def availability(self) -> TransportAvailability:
        return blocked_availability(self._id, self._surface, self._missing, f"{self._surface} is not authorized in this runtime")

    def bind_identity(self) -> dict[str, Any]:
        return {"transport_id": self._id, "bound_identity": self._identity, "vendor_session": False, "usable": False}

    def deliver(self) -> dict[str, Any]:
        return fail_closed(transport_id=self._id, missing_capability=self._missing, reason=f"{self._surface} unavailable")

    def acknowledge(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return self.deliver()

    def execute(self, message: dict[str, Any]) -> dict[str, Any]:
        del message
        return self.deliver()

    def cancel(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return self.deliver()

    def ingest_result(self, message_id: str, *, event_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self.deliver()
