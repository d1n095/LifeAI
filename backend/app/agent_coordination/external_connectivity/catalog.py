"""Official integration interfaces. Evidence-backed, not assumed.

Each row names the official surface, the identity it would authenticate, whether
an assignment-to-result round trip can be owned by MainAI, and why it is
AVAILABLE, ARCHITECTURE_READY, or BLOCKED in this runtime.

This catalog reuses the #302/#303 honesty contract and Claude's RAI external-
integration rules: unavailable vendor capabilities stay BLOCKED; a local CLI or
this Cloud Agent runtime is never reported as a vendor session.
"""

from __future__ import annotations

from typing import Any

from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.lifecycle import MISSING_MESSAGING, MISSING_PUBLICATION

CURSOR_DOCS = {
    "api_overview": "https://cursor.com/docs/api",
    "cloud_agents_overview": "https://cursor.com/docs/cloud-agent/api/overview",
    "cloud_agents_endpoints": "https://cursor.com/docs/cloud-agent/api/endpoints",
    "v0_webhooks": "https://cursor.com/docs/cloud-agent/api/webhooks",
    "service_accounts": "https://cursor.com/docs/account/enterprise/service-accounts",
}
CLAUDE_DOCS = {
    "authentication": "https://code.claude.com/docs/en/authentication",
    "headless": "https://code.claude.com/docs/en/headless",
    "agent_sdk": "https://code.claude.com/docs/en/agent-sdk/quickstart",
}
CODEX_DOCS = {
    "cli": "https://learn.chatgpt.com/docs/codex/cli.md",
    "auth": "https://learn.chatgpt.com/docs/auth.md",
    "github_repo": "https://github.com/openai/codex",
}

CURSOR_V1_ENDPOINTS = (
    ("POST", "/v1/agents", "create_agent_and_initial_run"),
    ("GET", "/v1/agents", "list_agents"),
    ("GET", "/v1/agents/{id}", "get_agent"),
    ("POST", "/v1/agents/{id}/runs", "follow_up_or_steer"),
    ("GET", "/v1/agents/{id}/runs", "list_runs"),
    ("GET", "/v1/agents/{id}/runs/{runId}", "get_run"),
    ("GET", "/v1/agents/{id}/runs/{runId}/stream", "stream_run"),
    ("POST", "/v1/agents/{id}/runs/{runId}/cancel", "cancel_run"),
    ("GET", "/v1/agents/{id}/usage", "get_usage"),
    ("GET", "/v1/agents/{id}/artifacts", "list_artifacts"),
    ("GET", "/v1/agents/{id}/artifacts/download", "download_artifact"),
    ("POST", "/v1/agents/{id}/archive", "archive_agent"),
    ("POST", "/v1/agents/{id}/unarchive", "unarchive_agent"),
    ("DELETE", "/v1/agents/{id}", "delete_agent"),
    ("GET", "/v1/me", "api_key_info"),
    ("GET", "/v1/models", "list_models"),
)

SELECTED_FIRST_PATH = "cursor_cloud_agents_api_v1"

STATUS_ARCHITECTURE_READY = "architecture_ready_not_live"
STATUS_BLOCKED = "blocked"
STATUS_LOCAL_ONLY = "local_authorized_not_vendor"


def official_interface_catalog() -> list[dict[str, Any]]:
    return [
        {
            "provider": "cursor",
            "surface": "cloud_agents_api_v1",
            "official": True,
            "docs": CURSOR_DOCS,
            "endpoints": [{"method": method, "path": path, "name": name} for method, path, name in CURSOR_V1_ENDPOINTS],
            "identity_class": "cursor_cloud_api_key_candidate",
            "identity_source": "Cursor Dashboard user API key or Enterprise service-account API key (Basic or Bearer).",
            "assignment_to_result": (
                "POST /v1/agents creates a durable agent and enqueues the first run; "
                "GET /v1/agents/{id} and GET /v1/agents/{id}/runs/{runId} observe status; "
                "POST /v1/agents/{id}/runs is the follow-up. v1 webhooks are 'coming soon'."
            ),
            "completion_evidence": (
                "Provider run status FINISHED/ERROR, optional artifact list, optional PR URL "
                "on the agent git metadata. Independently verifiable only with a provider-"
                "authenticated GET or a verified v0 HMAC webhook — not a local token."
            ),
            "round_trip_owner": "mainai_after_founder_authorization",
            "status": STATUS_ARCHITECTURE_READY,
            "usable_now": False,
            "missing_capability": "founder_cursor_cloud_agents_authorization",
            "selected_first_path": True,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "cursor",
            "surface": "cloud_agents_api_v0_webhooks",
            "official": True,
            "docs": {"webhooks": CURSOR_DOCS["v0_webhooks"]},
            "identity_class": "cursor_cloud_webhook_hmac",
            "identity_source": "X-Webhook-Signature HMAC-SHA256 over the raw body; User-Agent Cursor-Agent-Webhook/1.0.",
            "assignment_to_result": (
                "Legacy v0 statusChange events for ERROR and FINISHED. v1 webhooks are not GA."
            ),
            "completion_evidence": "HMAC-verified statusChange payload with id, status, optional target.prUrl/branchName/summary.",
            "round_trip_owner": "mainai_after_founder_webhook_secret_and_https_ingress",
            "status": STATUS_ARCHITECTURE_READY,
            "usable_now": False,
            "missing_capability": "founder_cursor_webhook_ingress",
            "selected_first_path": True,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "cursor",
            "surface": "local_cli",
            "official": True,
            "identity_class": "local_bridge_token",
            "identity_source": "Founder-enabled LIFE_AGENT_ADAPTER_* plus an installed cursor-agent binary.",
            "assignment_to_result": "Local subprocess only. Not a vendor session and not remote assignment delivery.",
            "completion_evidence": "Adapter heartbeat / process exit. Not provider-authenticated identity.",
            "round_trip_owner": "local_runtime",
            "status": STATUS_LOCAL_ONLY,
            "usable_now": False,
            "missing_capability": "founder_adapter_enablement",
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "cursor",
            "surface": "this_cloud_agent_runtime",
            "official": True,
            "identity_class": "this_cloud_agent_runtime",
            "identity_source": "CURSOR_AGENT / CURSOR_AGENT_SOCKET for *this* Cloud Agent.",
            "assignment_to_result": "Not an API MainAI can call to deliver work to another agent.",
            "completion_evidence": None,
            "round_trip_owner": "none",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": "not_a_delivery_api",
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "cursor",
            "surface": "private_vendor_session",
            "official": False,
            "identity_class": "none",
            "assignment_to_result": "No authorized Cursor private-session messaging adapter.",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": MISSING_MESSAGING,
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "claude",
            "surface": "headless_cli_and_agent_sdk",
            "official": True,
            "docs": CLAUDE_DOCS,
            "identity_class": "claude_code_provider_auth",
            "identity_source": "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN (paid Anthropic or subscription).",
            "assignment_to_result": (
                "claude -p and the Agent SDK run a local (or host-owned) process. There is no "
                "official Claude 'create remote coding agent + webhook completion' API that "
                "MainAI can own the way Cursor Cloud Agents API v1 can."
            ),
            "completion_evidence": "Process stdout/stderr. Provider identity is Anthropic auth, not a local bridge token.",
            "round_trip_owner": "blocked_paid_local_hosting",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": "claude_code_cli_and_paid_provider_auth",
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "claude",
            "surface": "private_vendor_session",
            "official": False,
            "identity_class": "none",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": MISSING_MESSAGING,
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "codex",
            "surface": "local_cli_exec",
            "official": True,
            "docs": CODEX_DOCS,
            "identity_class": "codex_provider_auth",
            "identity_source": "CODEX_API_KEY / ChatGPT login / OPENAI_API_KEY (paid).",
            "assignment_to_result": "codex exec is a local process. Not a MainAI-owned remote assignment API.",
            "completion_evidence": "Local process events. Not independently provider-remote.",
            "round_trip_owner": "blocked_paid_local_hosting",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": "codex_cli_and_paid_provider_auth",
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "codex",
            "surface": "github_coding_agent",
            "official": True,
            "identity_class": "codex_provider_auth",
            "identity_source": "GitHub App / Codex-owned publication authority.",
            "assignment_to_result": "Remote GitHub-hosted coding-agent writes. Out of Cursor scope.",
            "completion_evidence": "GitHub PR/check evidence — publication remains Codex-owned.",
            "round_trip_owner": "codex",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": MISSING_PUBLICATION,
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
        {
            "provider": "any",
            "surface": "provider_model_api",
            "official": True,
            "identity_class": "unknown_secret",
            "assignment_to_result": "Paid chat-completions APIs are not an assignment-to-result coding-agent path.",
            "status": STATUS_BLOCKED,
            "usable_now": False,
            "missing_capability": "provider_model_api",
            "selected_first_path": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        },
    ]


def selected_first_path() -> dict[str, Any]:
    rows = [row for row in official_interface_catalog() if row.get("selected_first_path")]
    return {
        "path_id": SELECTED_FIRST_PATH,
        "provider": "cursor",
        "surfaces": [row["surface"] for row in rows],
        "why": (
            "Cursor Cloud Agents API v1 is the only official, MainAI-ownable create + "
            "inspect + follow-up surface with a distinct provider identity (user or "
            "service-account API key) and a documented completion channel (run GET, plus "
            "legacy v0 HMAC webhooks for FINISHED/ERROR). Claude Code and Codex official "
            "interfaces are local CLIs or Codex-owned GitHub writes."
        ),
        "live_round_trip_possible_here": False,
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "rows": rows,
    }
