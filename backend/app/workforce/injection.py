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
    }
)


def scrub_authority_mutations(payload: Any) -> tuple[Any, list[str]]:
    """Recursively strip keys that attempt authority / policy mutation. Returns (cleaned, stripped)."""
    stripped: list[str] = []

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict = {}
            for key, value in node.items():
                lk = str(key).lower()
                if lk in FORBIDDEN_AUTHORITY_KEYS or any(f in lk for f in ("api_key", "vault", "secret", "credential")):
                    stripped.append(str(key))
                    continue
                out[key] = _walk(value)
            return out
        if isinstance(node, list):
            return [_walk(x) for x in node]
        return node

    return _walk(payload), stripped


def looks_like_prompt_injection(text: str) -> bool:
    """Heuristic detector for tests — not a complete defense; containment is structural."""
    lowered = text.lower()
    needles = (
        "ignore previous instructions",
        "you are now",
        "disclose the vault",
        "print the api key",
        "grant yourself",
        "widen your tools",
        "override mainai",
    )
    return any(n in lowered for n in needles)
