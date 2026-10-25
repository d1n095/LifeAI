"""Distinguish provider-authenticated identity from locally issued credentials.

A Founder HMAC bridge token proves possession of a MainAI-issued secret. It does
not authenticate a Cursor, Claude, or Codex vendor account. This Cloud Agent's
`CURSOR_AGENT` / `CURSOR_AGENT_SOCKET` variables describe *this* runtime, not a
delivery API. Provider identity requires a vendor-issued credential that this
module never logs and never treats as interchangeable with a local token.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any

from app.agent_coordination.durable import FIRST_CYCLE_GO

# Env *names* only. Values are never copied into returned structures.
PROVIDER_ENV_NAMES = {
    "cursor_cloud_api_key_candidate": ("CURSOR_API_KEY", "CURSOR_SERVICE_ACCOUNT_API_KEY"),
    "claude_code_provider_auth": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ),
    "codex_provider_auth": ("CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_ACCESS_TOKEN"),
}
LOCAL_BRIDGE_ENV_NAMES = ("LIFE_AGENT_BRIDGE_TOKEN",)
THIS_RUNTIME_ENV_NAMES = ("CURSOR_AGENT", "CURSOR_AGENT_SOCKET", "CURSOR_CONVERSATION_ID")
FOUNDER_ENABLE_CURSOR_CLOUD = "LIFE_CURSOR_CLOUD_AGENTS_ENABLED"
FOUNDER_ENABLE_LIVE_MUTATE = "LIFE_CURSOR_CLOUD_AGENTS_LIVE_MUTATE"
FOUNDER_WEBHOOK_SECRET_NAME = "LIFE_CURSOR_CLOUD_WEBHOOK_SECRET"


class IdentityClass(str, Enum):
    none = "none"
    local_bridge_token = "local_bridge_token"
    this_cloud_agent_runtime = "this_cloud_agent_runtime"
    cursor_cloud_api_key_candidate = "cursor_cloud_api_key_candidate"
    cursor_cloud_webhook_hmac = "cursor_cloud_webhook_hmac"
    claude_code_provider_auth = "claude_code_provider_auth"
    codex_provider_auth = "codex_provider_auth"
    unknown_secret = "unknown_secret"


PROVIDER_AUTHENTICATED_CLASSES = frozenset(
    {
        IdentityClass.cursor_cloud_api_key_candidate,
        IdentityClass.cursor_cloud_webhook_hmac,
        IdentityClass.claude_code_provider_auth,
        IdentityClass.codex_provider_auth,
    }
)
LOCAL_ONLY_CLASSES = frozenset(
    {
        IdentityClass.local_bridge_token,
        IdentityClass.this_cloud_agent_runtime,
        IdentityClass.none,
        IdentityClass.unknown_secret,
    }
)


def env_name_present(name: str, environ: dict[str, str] | None = None) -> bool:
    source = environ if environ is not None else os.environ
    return bool(str(source.get(name) or "").strip())


def present_env_names(*, names: tuple[str, ...], environ: dict[str, str] | None = None) -> list[str]:
    return [name for name in names if env_name_present(name, environ)]


def _looks_like_bridge_token(value: str) -> bool:
    if value.count(".") < 2:
        return False
    try:
        from app.agent_coordination.delivery import parse_bridge_token

        return parse_bridge_token(value) is not None
    except Exception:
        return False


def classify_credential(
    value: str | None,
    *,
    source: str | None = None,
    environ: dict[str, str] | None = None,
) -> IdentityClass:
    """Classify a credential by source and structure. Never returns the secret."""
    source_key = (source or "").strip()
    if source_key in LOCAL_BRIDGE_ENV_NAMES or source_key == "life_agent_bridge_token":
        return IdentityClass.local_bridge_token
    if source_key in THIS_RUNTIME_ENV_NAMES or source_key == "cursor_agent_socket":
        return IdentityClass.this_cloud_agent_runtime
    if source_key in PROVIDER_ENV_NAMES["cursor_cloud_api_key_candidate"]:
        return IdentityClass.cursor_cloud_api_key_candidate if str(value or "").strip() else IdentityClass.none
    if source_key in PROVIDER_ENV_NAMES["claude_code_provider_auth"]:
        return IdentityClass.claude_code_provider_auth if str(value or "").strip() else IdentityClass.none
    if source_key in PROVIDER_ENV_NAMES["codex_provider_auth"]:
        return IdentityClass.codex_provider_auth if str(value or "").strip() else IdentityClass.none
    if source_key == FOUNDER_WEBHOOK_SECRET_NAME:
        return IdentityClass.cursor_cloud_webhook_hmac if str(value or "").strip() else IdentityClass.none

    raw = str(value or "").strip()
    if not raw:
        return IdentityClass.none
    if source_key == "cursor_cloud_api_key" and _looks_like_bridge_token(raw):
        return IdentityClass.local_bridge_token
    if _looks_like_bridge_token(raw):
        return IdentityClass.local_bridge_token
    socket = (environ or os.environ).get("CURSOR_AGENT_SOCKET", "")
    if socket and raw == socket:
        return IdentityClass.this_cloud_agent_runtime
    if source_key == "cursor_cloud_api_key":
        return IdentityClass.cursor_cloud_api_key_candidate
    return IdentityClass.unknown_secret


def is_provider_authenticated(identity: IdentityClass) -> bool:
    return identity in PROVIDER_AUTHENTICATED_CLASSES


def local_token_is_not_provider_identity(identity: IdentityClass) -> bool:
    return identity in LOCAL_ONLY_CLASSES


def founder_flag(name: str, environ: dict[str, str] | None = None) -> bool:
    source = environ if environ is not None else os.environ
    return str(source.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def runtime_identity_observations(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Boolean presence of env *names*. Values are never included."""
    return {
        "this_cloud_agent_runtime": {
            "identity_class": IdentityClass.this_cloud_agent_runtime.value,
            "env_names_present": present_env_names(names=THIS_RUNTIME_ENV_NAMES, environ=environ),
            "is_delivery_api": False,
            "provider_authenticated": False,
            "note": "CURSOR_AGENT_SOCKET belongs to this Cloud Agent runtime, not a vendor delivery API.",
        },
        "local_bridge_token": {
            "identity_class": IdentityClass.local_bridge_token.value,
            "env_names_present": present_env_names(names=LOCAL_BRIDGE_ENV_NAMES, environ=environ),
            "is_delivery_api": False,
            "provider_authenticated": False,
            "note": "A Founder-issued LIFE_AGENT_BRIDGE_TOKEN authenticates the #302/#303 gateway, not Cursor/Claude/Codex.",
        },
        "cursor_cloud_api_key_candidate": {
            "identity_class": IdentityClass.cursor_cloud_api_key_candidate.value,
            "env_names_present": present_env_names(
                names=PROVIDER_ENV_NAMES["cursor_cloud_api_key_candidate"],
                environ=environ,
            ),
            "provider_authenticated": False,
            "founder_enabled": founder_flag(FOUNDER_ENABLE_CURSOR_CLOUD, environ),
            "live_mutate_enabled": founder_flag(FOUNDER_ENABLE_LIVE_MUTATE, environ),
            "note": "Presence of an env name is not Founder authorization and is not a completed vendor round trip.",
        },
        "claude_code_provider_auth": {
            "identity_class": IdentityClass.claude_code_provider_auth.value,
            "env_names_present": present_env_names(
                names=PROVIDER_ENV_NAMES["claude_code_provider_auth"],
                environ=environ,
            ),
            "provider_authenticated": False,
        },
        "codex_provider_auth": {
            "identity_class": IdentityClass.codex_provider_auth.value,
            "env_names_present": present_env_names(
                names=PROVIDER_ENV_NAMES["codex_provider_auth"],
                environ=environ,
            ),
            "provider_authenticated": False,
        },
        "first_cycle_go": FIRST_CYCLE_GO,
        "vendor_delivery_claimed": False,
    }
