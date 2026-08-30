"""Agent security / injection containment (T15).

External agent/provider output is DATA. It cannot change authority, modify system policy,
request secrets, grant itself tools, create stronger agents, or override MainAI/founder.
"""

from __future__ import annotations

from typing import Any

FORBIDDEN_AUTHORITY_KEYS: frozenset[str] = frozenset(
    {
        "grant_authority",
        "widen_tools",
        "allowed_tool_classes",
        "allowed_write_paths",
        "spend_ceiling_usd",
        "trust_zone",
        "role",
        "status",
        "create_agent",
        "elevate_agent",
        "request_vault",
        "request_api_key",
        "request_secret",
        "override_mainai",
        "override_founder",
        "modify_system_policy",
        "self_verify",
        "set_verification_status",
        "verification_status",
        "create_subagent",
        "stronger_subagent",
        "change_own_role",
        "access_other_worker_context",
        "ignore_policy",
        "ignore_previous_instructions",
    }
)

INJECTION_NEEDLES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore policy",
    "you are now",
    "disclose the vault",
    "print the api key",
    "grant yourself",
    "widen your tools",
    "override mainai",
    "create a stronger agent",
    "change your role to",
    "exfiltrate",
)


def scrub_authority_mutations(payload: Any) -> tuple[Any, list[str]]:
    """Recursively strip keys that attempt authority / policy mutation. Returns (cleaned, stripped)."""
    stripped: list[str] = []

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict = {}
            for key, value in node.items():
                lk = str(key).lower()
                if lk in FORBIDDEN_AUTHORITY_KEYS or any(
                    f in lk for f in ("api_key", "vault", "secret", "credential", "subagent", "widen")
                ):
                    stripped.append(str(key))
                    continue
                out[key] = _walk(value)
            return out
        if isinstance(node, list):
            return [_walk(x) for x in node]
        if isinstance(node, str) and looks_like_prompt_injection(node):
            stripped.append("__injection_string__")
            return "[REDACTED_INJECTION]"
        return node

    return _walk(payload), stripped


def looks_like_prompt_injection(text: str) -> bool:
    """Heuristic detector for tests — containment remains structural (scrub + deny)."""
    lowered = text.lower()
    return any(n in lowered for n in INJECTION_NEEDLES)


def fail_closed_on_secret_request(payload: dict) -> None:
    """Raise if payload still contains secret solicitation after scrub would be required."""
    raw = str(payload).lower()
    for needle in ("vault", "api_key", "api key", "secret", "credential"):
        if needle in raw and "redacted" not in raw:
            # Structural callers should scrub first; this is a hard gate for tests.
            from app.workforce.broker import DelegationBrokerError

            raise DelegationBrokerError(f"fail-closed: secret solicitation detected ({needle})")


def refuse_role_or_tool_self_upgrade(payload: dict) -> list[str]:
    """Return list of refused upgrade attempts found in payload keys/values."""
    refused: list[str] = []
    blob = str(payload).lower()
    for phrase in (
        "widen tools",
        "change own role",
        "create stronger",
        "elevate",
        "grant yourself",
    ):
        if phrase in blob:
            refused.append(phrase)
    return refused
