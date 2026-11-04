"""Fail-closed Cursor Cloud Agents client.

Official create / follow-up / inspect live on https://api.cursor.com/v1.
This module implements the gates and the official v0 webhook HMAC. It does not
mint a vendor PASS, does not spend Founder quota, and does not treat a local
bridge token or CURSOR_AGENT_SOCKET as provider identity.

Live mutating calls stay BLOCKED even if a key name is present: this candidate
is not authorized to make paid API calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.external_connectivity.identity import (
    FOUNDER_ENABLE_CURSOR_CLOUD,
    FOUNDER_ENABLE_LIVE_MUTATE,
    IdentityClass,
    classify_credential,
    founder_flag,
    is_provider_authenticated,
)

API_BASE = "https://api.cursor.com"
CREATE_PATH = "/v1/agents"
ME_PATH = "/v1/me"
WEBHOOK_USER_AGENT = "Cursor-Agent-Webhook/1.0"
MISSING_PROVIDER_KEY = "founder_cursor_cloud_agents_authorization"
MISSING_LIVE_MUTATE = "founder_cursor_cloud_live_mutate_authorization"
MISSING_WEBHOOK_SECRET = "founder_cursor_webhook_ingress"
MISSING_NOT_PROVIDER = "provider_identity_required"


def _blocked(*, missing_capability: str, reason: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "outcome": "blocked",
        "transport_id": "cursor_cloud_agents_api_v1",
        "missing_capability": missing_capability,
        "reason": reason,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "live_http_attempted": False,
    }
    if extra:
        row.update(extra)
    return row


def authorize_provider_key(
    api_key: str | None,
    *,
    source: str | None = None,
    founder_authorized: bool = False,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    identity = classify_credential(api_key, source=source or "cursor_cloud_api_key", environ=environ)
    if identity is IdentityClass.local_bridge_token:
        return _blocked(
            missing_capability=MISSING_NOT_PROVIDER,
            reason="A locally issued bridge token is not a Cursor Cloud Agents API key.",
            extra={"identity_class": identity.value, "provider_authenticated": False},
        )
    if identity is IdentityClass.this_cloud_agent_runtime:
        return _blocked(
            missing_capability="not_a_delivery_api",
            reason="CURSOR_AGENT_SOCKET is this runtime, not a provider API key.",
            extra={"identity_class": identity.value, "provider_authenticated": False},
        )
    if identity is not IdentityClass.cursor_cloud_api_key_candidate:
        return _blocked(
            missing_capability=MISSING_PROVIDER_KEY,
            reason="No Cursor Cloud Agents API key candidate was supplied.",
            extra={"identity_class": identity.value, "provider_authenticated": False},
        )
    if not founder_authorized and not founder_flag(FOUNDER_ENABLE_CURSOR_CLOUD, environ):
        return _blocked(
            missing_capability=MISSING_PROVIDER_KEY,
            reason="Founder has not enabled LIFE_CURSOR_CLOUD_AGENTS_ENABLED.",
            extra={"identity_class": identity.value, "provider_authenticated": False},
        )
    return {
        "outcome": "authorized_candidate",
        "identity_class": identity.value,
        "provider_authenticated": is_provider_authenticated(identity),
        "vendor_delivery_claimed": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "note": "Candidate accepted as a provider-key *class* only. No live call is implied.",
    }


class CursorCloudAgentClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        source: str | None = None,
        founder_authorized: bool = False,
        environ: dict[str, str] | None = None,
        api_base: str = API_BASE,
    ) -> None:
        self._api_key = api_key
        self._source = source
        self._founder_authorized = founder_authorized
        self._environ = environ
        self._api_base = api_base.rstrip("/")

    def _gate(self, *, mutate: bool) -> dict[str, Any] | None:
        auth = authorize_provider_key(
            self._api_key,
            source=self._source,
            founder_authorized=self._founder_authorized,
            environ=self._environ,
        )
        if auth.get("outcome") != "authorized_candidate":
            return auth
        if mutate:
            live = founder_flag(FOUNDER_ENABLE_LIVE_MUTATE, self._environ)
            if not live:
                return _blocked(
                    missing_capability=MISSING_LIVE_MUTATE,
                    reason=(
                        "Live POST/DELETE against api.cursor.com is not authorized on this "
                        "candidate. No paid Cloud Agent create/follow-up is attempted."
                    ),
                    extra={"identity_class": auth["identity_class"], "provider_authenticated": False},
                )
            return _blocked(
                missing_capability=MISSING_LIVE_MUTATE,
                reason="Paid Cloud Agent mutation stays fail-closed even when the enable flag is set.",
                extra={"identity_class": auth["identity_class"], "hard_block": True},
            )
        return None

    def create_agent(self, prompt_text: str, **_kwargs: Any) -> dict[str, Any]:
        del prompt_text
        return self._gate(mutate=True) or _blocked(
            missing_capability=MISSING_LIVE_MUTATE,
            reason="create_agent is fail-closed.",
        )

    def follow_up(self, agent_id: str, prompt_text: str, **_kwargs: Any) -> dict[str, Any]:
        del agent_id, prompt_text
        return self._gate(mutate=True) or _blocked(
            missing_capability=MISSING_LIVE_MUTATE,
            reason="follow_up is fail-closed.",
        )

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        del agent_id
        blocked = self._gate(mutate=False)
        if blocked:
            return blocked
        return _blocked(
            missing_capability=MISSING_LIVE_MUTATE,
            reason="Authenticated GET is reserved for a Founder-authorized live probe; this prototype does not send the key.",
        )


def verify_v0_webhook(
    *,
    secret: str,
    raw_body: bytes,
    signature: str,
    user_agent: str | None = None,
    fixture: bool = False,
) -> dict[str, Any]:
    """Official HMAC-SHA256 check from Cursor Cloud Agents v0 webhook docs."""
    if not secret:
        return _blocked(
            missing_capability=MISSING_WEBHOOK_SECRET,
            reason="No webhook secret. HMAC cannot authenticate Cursor.",
        )
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(expected, str(signature or ""))
    agent_ok = user_agent in (None, WEBHOOK_USER_AGENT)
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not valid or not agent_ok:
        return {
            "outcome": "rejected",
            "provider_authenticated": False,
            "identity_class": IdentityClass.cursor_cloud_webhook_hmac.value,
            "reason": "Webhook signature or User-Agent did not match the official Cursor v0 contract.",
            "vendor_delivery_claimed": False,
            "independently_verified": False,
            "fixture": fixture,
            "first_cycle_go": FIRST_CYCLE_GO,
        }
    return {
        "outcome": "accepted",
        "provider_authenticated": not fixture,
        "identity_class": IdentityClass.cursor_cloud_webhook_hmac.value,
        "event": payload.get("event"),
        "status": payload.get("status"),
        "agent_id": payload.get("id"),
        "webhook_event": True,
        "fixture": fixture,
        "vendor_delivery_claimed": False,
        "independently_verified": False,
        "first_cycle_go": FIRST_CYCLE_GO,
        "payload": payload,
        "note": (
            "HMAC matched a caller-supplied secret. A fixture is not a real Cursor delivery. "
            "A non-fixture acceptance still needs Founder ingress before it is live evidence."
        ),
    }


def probe_unauthenticated_surface(*, timeout_seconds: float = 8.0) -> dict[str, Any]:
    """Credential-free GET. A 401/403 proves the official host exists. Never a delivery."""
    url = f"{API_BASE}{ME_PATH}"
    request = Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "LifeAI-external-connectivity-probe/1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 — fixed official HTTPS host
            status = int(response.status)
            body = response.read(256)
    except HTTPError as exc:
        status = int(exc.code)
        body = exc.read(256) if exc.fp else b""
    except URLError as exc:
        return {
            "outcome": "blocked",
            "reachable": False,
            "reason": f"Official host not reached: {exc.reason}",
            "url": url,
            "vendor_delivery_claimed": False,
            "live_http_attempted": True,
            "authenticated": False,
            "first_cycle_go": FIRST_CYCLE_GO,
        }
    auth_required = status in {401, 403}
    return {
        "outcome": "surface_observed" if auth_required else "unexpected_status",
        "reachable": True,
        "http_status": status,
        "auth_required": auth_required,
        "authenticated": False,
        "url": url,
        "body_prefix_len": len(body),
        "vendor_delivery_claimed": False,
        "live_http_attempted": True,
        "first_cycle_go": FIRST_CYCLE_GO,
        "note": "Unauthenticated probe only. No API key was sent and no agent was created.",
    }
