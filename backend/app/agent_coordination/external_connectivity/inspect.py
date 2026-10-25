"""Runtime inspection of official coding-agent surfaces. Never simulates delivery."""

from __future__ import annotations

import shutil
from typing import Any

from app.agent_coordination.adapter_config import SUPPORTED_LOCAL_CLI_PROVIDERS, adapter_availability
from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.external_connectivity.catalog import official_interface_catalog, selected_first_path
from app.agent_coordination.external_connectivity.cursor_cloud import CursorCloudAgentClient, probe_unauthenticated_surface
from app.agent_coordination.external_connectivity.identity import runtime_identity_observations
from app.agent_coordination.lifecycle import MISSING_MESSAGING

LOCAL_BINARIES = {
    "cursor-agent": "cursor",
    "claude": "claude",
    "codex": "codex",
}


def inspect_local_binaries() -> dict[str, Any]:
    rows = {}
    for binary, provider in LOCAL_BINARIES.items():
        path = shutil.which(binary)
        rows[binary] = {
            "provider": provider,
            "executable_found": path is not None,
            "path_present": path is not None,
            "vendor_session": False,
        }
    for key in SUPPORTED_LOCAL_CLI_PROVIDERS:
        avail = adapter_availability(key)
        rows[key] = {
            "provider": key,
            "supported": avail.supported,
            "executable_found": avail.executable_found,
            "enabled": avail.enabled,
            "credentials_state": avail.credentials_state,
            "vendor_session": False,
        }
    return rows


def inspect_external_connectivity(
    *,
    probe_network: bool = False,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    catalog = official_interface_catalog()
    first = selected_first_path()
    identities = runtime_identity_observations(environ)
    client = CursorCloudAgentClient(api_key=None, environ=environ)
    create_blocked = client.create_agent("must not run")
    probe = probe_unauthenticated_surface() if probe_network else {
        "outcome": "skipped",
        "live_http_attempted": False,
        "vendor_delivery_claimed": False,
        "note": "Network probe is opt-in. Inspection never sends credentials.",
    }
    return {
        "catalog": catalog,
        "selected_first_path": first,
        "identities": identities,
        "local_binaries": inspect_local_binaries(),
        "cursor_cloud_create": create_blocked,
        "unauthenticated_probe": probe,
        "blocked_providers": [
            row
            for row in catalog
            if row["provider"] in {"claude", "codex"} or row["surface"] in {"private_vendor_session", "provider_model_api"}
        ],
        "external_vendor_operations": {
            "usable": False,
            "cursor": False,
            "claude": False,
            "codex": False,
            "missing_capability": MISSING_MESSAGING,
            "architecture_ready": first["path_id"],
        },
        "remaining_authorization": [
            "Founder-issued Cursor user or service-account API key (not a local bridge token).",
            "LIFE_CURSOR_CLOUD_AGENTS_ENABLED=true after reviewing spend.",
            "Separate Founder authorization for any live mutate (create/follow-up) — currently hard-blocked.",
            "HTTPS webhook ingress plus LIFE_CURSOR_CLOUD_WEBHOOK_SECRET for v0 statusChange HMAC.",
            "Bind provider agent.id to an existing AgentWorkAssignment on the #302/#303 ledger.",
            "Independent examiner identity for independently_verified — builder must not self-assert.",
            "Claude Code CLI + paid Anthropic auth remain BLOCKED.",
            "Codex CLI / GitHub coding-agent writes remain Codex-owned and BLOCKED here.",
        ],
        "next_engineering_steps": [
            "Wait for Claude's independent disposition of #306. Do not stack this work on uncertified #306 code.",
            "After #306 disposition, compose this client into the certified AgentTransport registry as cursor_cloud_agents_api_v1.",
            "Founder provisions a scoped Cursor API key and webhook secret off-chat.",
            "Run one Founder-supervised create + GET + HMAC (or GET-only) round trip against a throwaway repo.",
            "Record provider agent.id/run.id on the existing durable ledger; keep vendor_delivery_claimed false until that evidence exists.",
        ],
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "amends_303_through_306": False,
    }
