"""Generic AgentTransport — messaging/execution over the existing durable gateway.

This is not a second inbox. Vendor-specific behavior lives in isolated adapters.
Generic receive/ack/execute/complete logic keys transports by id, never by a
hardcoded vendor name. One surface is never reported as another.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from app.agent_coordination.durable import FIRST_CYCLE_GO

SURFACES = (
    "local_cli",
    "agent_side_runner",
    "provider_model_api",
    "remote_background_agent",
    "private_vendor_session",
)

MISSING_VENDOR_SESSION = "vendor_private_session"
MISSING_MODEL_API = "provider_model_api"
MISSING_REMOTE = "remote_background_agent"
MISSING_RUNNER_AUTH = "founder_agent_side_authorization"


@dataclass(frozen=True)
class TransportCapabilities:
    deliver: bool = False
    acknowledge: bool = False
    execute: bool = False
    cancel: bool = False
    ingest_result: bool = False
    vendor_session: bool = False
    model_api: bool = False
    remote_background: bool = False
    write_scope: str = "none"


@dataclass(frozen=True)
class TransportAvailability:
    transport_id: str
    surface: str
    installed: bool
    configured: bool
    founder_authorized: bool
    reachable: bool
    missing_capability: str | None
    reason: str
    vendor_delivery_claimed: bool = False
    first_cycle_go: bool = FIRST_CYCLE_GO


class AgentTransport(Protocol):
    def transport_id(self) -> str: ...

    def capabilities(self) -> TransportCapabilities: ...

    def availability(self) -> TransportAvailability: ...

    def bind_identity(self) -> dict[str, Any]: ...

    def deliver(self) -> dict[str, Any]: ...

    def acknowledge(self, message_id: str, *, event_id: str) -> dict[str, Any]: ...

    def execute(self, message: dict[str, Any]) -> dict[str, Any]: ...

    def cancel(self, message_id: str, *, event_id: str) -> dict[str, Any]: ...

    def ingest_result(self, message_id: str, *, event_id: str, result: dict[str, Any]) -> dict[str, Any]: ...


def public_availability(row: TransportAvailability) -> dict[str, Any]:
    return asdict(row)


def public_capabilities(caps: TransportCapabilities) -> dict[str, Any]:
    return asdict(caps)


def fail_closed(*, transport_id: str, missing_capability: str, reason: str) -> dict[str, Any]:
    return {
        "outcome": "blocked",
        "transport_id": transport_id,
        "missing_capability": missing_capability,
        "reason": reason,
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


@dataclass
class TransportRegistry:
    """Idempotent id → factory. Generic code never switches on vendor names."""

    _factories: dict[str, Any] = field(default_factory=dict)

    def register(self, transport_id: str, factory: Any) -> None:
        self._factories[transport_id] = factory

    def get(self, transport_id: str) -> Any | None:
        return self._factories.get(transport_id)

    def ids(self) -> list[str]:
        return sorted(self._factories)


_REGISTRY = TransportRegistry()


def registry() -> TransportRegistry:
    if not _REGISTRY.ids():
        from app.agent_coordination.transports import register_default_transports

        register_default_transports(_REGISTRY)
    return _REGISTRY


def capability_matrix() -> list[dict[str, Any]]:
    """Honest inventory of every inspected surface. Never equates CLI with a vendor session."""
    from app.agent_coordination.transports import inspect_surfaces

    rows = inspect_surfaces()
    for row in rows:
        row["first_cycle_go"] = FIRST_CYCLE_GO
        row["vendor_delivery_claimed"] = False
    return rows
