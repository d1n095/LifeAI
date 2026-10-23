"""Connect the existing delivery gateway to actually available authorized transports.

This is not a second messaging subsystem. It inventories local CLI adapters and the
Founder-issued local authorized bridge, then uses the existing inbox/claim/receipt APIs.
Cursor/Claude/Codex vendor APIs stay BLOCKED with a named missing capability and a
concrete Founder setup requirement.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Callable

from app.agent_coordination.adapter_config import (
    SUPPORTED_LOCAL_CLI_PROVIDERS,
    adapter_availability,
    real_adapter_config,
    resolve_adapter_env,
)
from app.agent_coordination.delivery import FIRST_CYCLE_GO, delivery_public_view, ensure_delivery
from app.agent_coordination.durable import DurableCoordinationService
from app.agent_coordination.lifecycle import MISSING_MESSAGING, MISSING_PUBLICATION
from app.agent_coordination.local_bridge import LocalAuthorizedBridge

RECIPIENT_TO_PROVIDER = {
    "cursor": "cursor-agent",
    "claude": "claude-code",
    "codex": "codex",
}

VENDOR_SETUP = {
    "cursor": {
        "missing_capability": MISSING_MESSAGING,
        "founder_setup": [
            "Issue a scoped local bridge: POST /api/mainai/founder-ops/agent-gateway/bridges {\"recipient\":\"cursor\"}",
            "Run LIFE_AGENT_BRIDGE_TOKEN=... python -m app.agent_coordination.local_bridge",
            "Optional local CLI execution (not vendor messaging): LIFE_AGENT_ADAPTER_ENABLED__CURSOR_AGENT=true",
            "LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CURSOR_AGENT=true",
            "LIFE_AGENT_ADAPTER_ARGS__CURSOR_AGENT=<founder argv template>",
            "No Cursor vendor workspace/API is available in this runtime",
        ],
    },
    "claude": {
        "missing_capability": MISSING_MESSAGING,
        "founder_setup": [
            "Issue a scoped local bridge: POST /api/mainai/founder-ops/agent-gateway/bridges {\"recipient\":\"claude\"}",
            "Run LIFE_AGENT_BRIDGE_TOKEN=... python -m app.agent_coordination.local_bridge",
            "Optional local CLI execution (not vendor messaging): LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE=true",
            "LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CLAUDE_CODE=true",
            "LIFE_AGENT_ADAPTER_ARGS__CLAUDE_CODE=<founder argv template>",
            "No Claude vendor messaging API is available in this runtime",
        ],
    },
    "codex": {
        "missing_capability": MISSING_MESSAGING,
        "founder_setup": [
            "Issue a scoped local bridge: POST /api/mainai/founder-ops/agent-gateway/bridges {\"recipient\":\"codex\"}",
            "Run LIFE_AGENT_BRIDGE_TOKEN=... python -m app.agent_coordination.local_bridge",
            "Optional local CLI execution (not vendor messaging): LIFE_AGENT_ADAPTER_ENABLED__CODEX=true",
            "LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CODEX=true",
            "LIFE_AGENT_ADAPTER_ARGS__CODEX=<founder argv template>",
            "No Codex vendor messaging API is available; GitHub publication stays Codex-owned",
        ],
    },
}


def founder_setup_requirements() -> list[dict[str, Any]]:
    rows = []
    for recipient, spec in VENDOR_SETUP.items():
        provider = RECIPIENT_TO_PROVIDER[recipient]
        avail = adapter_availability(provider)
        rows.append(
            {
                "recipient": recipient,
                "vendor_messaging_usable": False,
                "missing_capability": spec["missing_capability"],
                "founder_setup": spec["founder_setup"],
                "local_cli": {
                    "provider": provider,
                    "supported": avail.supported,
                    "enabled": avail.enabled,
                    "executable_found": avail.executable_found,
                    "executable_path": avail.executable_path,
                    "credentials_state": avail.credentials_state,
                    "config_ready": real_adapter_config(provider) is not None,
                    "reason": avail.reason,
                },
                "vendor_delivery_claimed": False,
                "first_cycle_go": FIRST_CYCLE_GO,
            }
        )
    rows.append(
        {
            "recipient": "github_publication",
            "vendor_messaging_usable": False,
            "missing_capability": MISSING_PUBLICATION,
            "founder_setup": ["GitHub publication and grants remain Codex-owned. Do not use personal credentials."],
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }
    )
    return rows


def run_founder_local_cli(provider_key: str) -> dict[str, Any]:
    """Invoke a Founder-enabled local CLI only. Never a vendor API or personal OAuth."""
    if provider_key not in SUPPORTED_LOCAL_CLI_PROVIDERS:
        return {"ran": False, "missing_capability": "founder_adapter_enablement", "vendor_delivery_claimed": False}
    config = real_adapter_config(provider_key)
    if config is None:
        avail = adapter_availability(provider_key)
        return {
            "ran": False,
            "missing_capability": "founder_adapter_enablement",
            "reason": avail.reason,
            "vendor_delivery_claimed": False,
        }
    executable, args, timeout = config
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(resolve_adapter_env(provider_key))
    completed = subprocess.run(
        [executable, *args],
        timeout=min(timeout, 30),
        capture_output=True,
        env=env,
        check=False,
    )
    return {
        "ran": True,
        "provider": provider_key,
        "returncode": completed.returncode,
        "vendor_delivery_claimed": False,
        "evidence_kind": "adapter_heartbeat",
    }


def run_authorized_receiver(
    *,
    token: str,
    request: Callable[..., Any],
    execute_local_cli: bool = False,
    recipient: str | None = None,
) -> dict[str, Any]:
    """Use the existing gateway APIs. Optional Founder-enabled local CLI after a real claim."""
    bridge = LocalAuthorizedBridge(token=token, request=request)
    delivered = bridge.pull()
    receipts = []
    for row in delivered:
        message = row.get("message") or {}
        message_id = str(message.get("message_id") or "")
        identity = recipient or str(message.get("recipient") or "")
        ack = bridge.acknowledge(message_id, event_id=f"connect-ack-{message_id[:8]}")
        cli = {"ran": False}
        if execute_local_cli and identity in RECIPIENT_TO_PROVIDER:
            cli = run_founder_local_cli(RECIPIENT_TO_PROVIDER[identity])
            if cli.get("ran"):
                bridge.report_execution(
                    message_id,
                    event_id=f"connect-exec-{message_id[:8]}",
                    evidence={"kind": "adapter_heartbeat", "source": "local_cli", "provider": cli.get("provider")},
                )
        complete = bridge.complete(message_id, event_id=f"connect-done-{message_id[:8]}", evidence=cli)
        receipts.append({"delivered": row, "ack": ack, "cli": cli, "complete": complete})
    return {
        "delivered_count": len(delivered),
        "receipts": receipts,
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
    }


def operational_handover_view(service: DurableCoordinationService) -> dict[str, Any]:
    """Evidence-backed answer: can MainAI coordinate without Founder transcription?"""
    delivery = delivery_public_view(service)
    github = service.github_intake_view()
    obligations = list((getattr(service, "_obligations", None) or {}).values())
    messages = delivery.get("messages") or []
    delivered = [row for row in messages if row.get("status") in {"delivered", "acknowledged", "executing", "completed"}]
    acknowledged = [row for row in messages if row.get("acknowledged_at") or row.get("status") in {"acknowledged", "executing", "completed"}]
    completed = [row for row in messages if row.get("status") == "completed"]
    examiners = [row for row in obligations if row.get("agent_identity") == "examiner"]
    live_bridge = any(row.get("live") for row in (delivery.get("bridges") or []))
    internal = bool(obligations) and bool(github.get("candidates") or obligations)
    local_bridge = bool(delivered) and live_bridge
    vendor = False
    founder_must_forward = not (internal and (local_bridge or completed))
    return {
        "question": (
            "Can MainAI receive development updates, select the correct agent, deliver the "
            "next instruction, receive a completion report and initiate independent review "
            "without the Founder manually forwarding messages?"
        ),
        "internal_coordination": {
            "usable": internal,
            "obligation_count": len(obligations),
            "examiner_prepared": bool(examiners),
            "github_candidates": len(github.get("candidates") or []),
        },
        "local_authorized_bridge": {
            "usable": local_bridge or bool(delivered),
            "delivered": len(delivered),
            "acknowledged": len(acknowledged),
            "completed": len(completed),
        },
        "external_vendor_operations": {
            "usable": vendor,
            "cursor": False,
            "claude": False,
            "codex": False,
            "missing_capability": MISSING_MESSAGING,
        },
        "founder_must_forward_messages": founder_must_forward,
        "answer": (
            "Internal coordination and the Founder-issued local authorized bridge can receive "
            "updates, deliver one instruction, ingest a completion and prepare independent "
            "review. Cursor/Claude/Codex vendor messaging remains disconnected."
            if internal
            else "Internal coordination is not yet persisted; vendor messaging is disconnected."
        ),
        "remaining_permissions": founder_setup_requirements(),
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "adapters": delivery.get("adapters") or [],
    }


def attach_handover(service: DurableCoordinationService) -> dict[str, Any]:
    ensure_delivery(service)
    return operational_handover_view(service)
