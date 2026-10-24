"""Isolated AgentTransport adapters. Generic callers use the registry, not vendor names."""

from __future__ import annotations

from typing import Any

from app.agent_coordination.adapter_config import SUPPORTED_LOCAL_CLI_PROVIDERS, adapter_availability
from app.agent_coordination.lifecycle import MISSING_MESSAGING, MISSING_PUBLICATION
from app.agent_coordination.transport import (
    MISSING_MODEL_API,
    MISSING_REMOTE,
    MISSING_VENDOR_SESSION,
    TransportAvailability,
    TransportRegistry,
)

GATEWAY_RUNNER_ID = "authorized_gateway_runner"
LOCAL_CLI_ID = "local_cli_execution"
VENDOR_SESSION_PREFIX = "vendor_session_"
MODEL_API_ID = "provider_model_api"
REMOTE_ID = "remote_background_agent"


def register_default_transports(registry: TransportRegistry) -> None:
    from app.agent_coordination.transports.gateway import AuthorizedGatewayTransport
    from app.agent_coordination.transports.local_cli import LocalCliTransport
    from app.agent_coordination.transports.vendor import BlockedVendorTransport

    registry.register(GATEWAY_RUNNER_ID, AuthorizedGatewayTransport)
    registry.register(LOCAL_CLI_ID, LocalCliTransport)
    registry.register(MODEL_API_ID, lambda **kwargs: BlockedVendorTransport(transport_id=MODEL_API_ID, surface="provider_model_api", missing=MISSING_MODEL_API, **kwargs))
    registry.register(REMOTE_ID, lambda **kwargs: BlockedVendorTransport(transport_id=REMOTE_ID, surface="remote_background_agent", missing=MISSING_REMOTE, **kwargs))
    for identity in ("cursor", "claude", "codex"):
        transport_id = f"{VENDOR_SESSION_PREFIX}{identity}"
        registry.register(
            transport_id,
            lambda identity=identity, transport_id=transport_id, **kwargs: BlockedVendorTransport(
                transport_id=transport_id,
                surface="private_vendor_session",
                missing=MISSING_VENDOR_SESSION,
                bound_identity=identity,
                **kwargs,
            ),
        )


def inspect_surfaces() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "transport_id": GATEWAY_RUNNER_ID,
            "surface": "agent_side_runner",
            "usable": True,
            "installed": True,
            "configured": True,
            "founder_authorized": False,
            "reachable": False,
            "note": "Founder-issued gateway token + python -m app.agent_coordination.agent_side. Not a vendor session.",
            "missing_capability": None,
        },
        {
            "transport_id": LOCAL_CLI_ID,
            "surface": "local_cli",
            "usable": False,
            "installed": any(adapter_availability(key).executable_found for key in SUPPORTED_LOCAL_CLI_PROVIDERS),
            "configured": False,
            "founder_authorized": False,
            "reachable": False,
            "note": "Local CLI execution is not vendor messaging and is not a private session.",
            "missing_capability": "founder_adapter_enablement",
            "providers": {
                key: {
                    "supported": adapter_availability(key).supported,
                    "executable_found": adapter_availability(key).executable_found,
                    "enabled": adapter_availability(key).enabled,
                    "credentials_state": adapter_availability(key).credentials_state,
                }
                for key in SUPPORTED_LOCAL_CLI_PROVIDERS
            },
        },
        {
            "transport_id": MODEL_API_ID,
            "surface": "provider_model_api",
            "usable": False,
            "installed": False,
            "configured": False,
            "founder_authorized": False,
            "reachable": False,
            "missing_capability": MISSING_MODEL_API,
            "note": "Paid provider chat APIs are out of scope and are not invoked.",
        },
        {
            "transport_id": REMOTE_ID,
            "surface": "remote_background_agent",
            "usable": False,
            "installed": False,
            "configured": False,
            "founder_authorized": False,
            "reachable": False,
            "missing_capability": MISSING_REMOTE,
            "note": "No authorized Cursor/Claude/Codex remote coding-agent API is available.",
        },
        {
            "transport_id": "github_publication",
            "surface": "privileged_write",
            "usable": False,
            "missing_capability": MISSING_PUBLICATION,
            "note": "GitHub publication remains Codex-owned.",
        },
    ]
    for identity in ("cursor", "claude", "codex"):
        rows.append(
            {
                "transport_id": f"{VENDOR_SESSION_PREFIX}{identity}",
                "surface": "private_vendor_session",
                "bound_identity": identity,
                "usable": False,
                "installed": False,
                "configured": False,
                "founder_authorized": False,
                "reachable": False,
                "missing_capability": MISSING_VENDOR_SESSION,
                "note": (
                    f"No authorized {identity} private-session or vendor messaging adapter. "
                    "A local CLI binary, if later Founder-enabled, is still not this surface."
                ),
                "also_missing": MISSING_MESSAGING,
            }
        )
    return rows


def blocked_availability(transport_id: str, surface: str, missing: str, reason: str) -> TransportAvailability:
    return TransportAvailability(
        transport_id=transport_id,
        surface=surface,
        installed=False,
        configured=False,
        founder_authorized=False,
        reachable=False,
        missing_capability=missing,
        reason=reason,
    )
