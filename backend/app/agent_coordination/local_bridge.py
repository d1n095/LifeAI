"""Authorized local agent bridge.

This is the one genuinely executable messaging transport. It pulls this owner's
recipient inbox over the production HTTP gateway using a Founder-issued scoped
token. It does not call Cursor, Claude, or Codex vendor APIs and never uses
personal credentials.

Run: LIFE_AGENT_BRIDGE_TOKEN=... LIFE_AGENT_BRIDGE_BASE_URL=http://127.0.0.1:8000 \\
     python -m app.agent_coordination.local_bridge
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

import httpx


class LocalAuthorizedBridge:
    """Pulls and acknowledges work through the production agent-gateway routes."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "http://127.0.0.1:8000",
        transport: httpx.BaseTransport | None = None,
        request: Callable[..., Any] | None = None,
    ) -> None:
        if not token:
            raise ValueError("Founder-issued bridge token is required")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = None
        self._request = request
        if request is None:
            self._client = httpx.Client(base_url=self.base_url, transport=transport, timeout=15.0)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._request is not None:
            return self._request(method, path, headers=self._headers(), **kwargs)
        assert self._client is not None
        return self._client.request(method, path, headers=self._headers(), **kwargs)

    def list_inbox(self) -> list[dict[str, Any]]:
        response = self._call("GET", "/api/mainai/agent-gateway/inbox")
        if getattr(response, "status_code", 0) != 200:
            raise RuntimeError(f"inbox list failed: {getattr(response, 'status_code', '?')}")
        payload = response.json()
        return list(payload.get("messages") or [])

    def claim(self, message_id: str) -> dict[str, Any]:
        response = self._call("POST", f"/api/mainai/agent-gateway/inbox/{message_id}/claim")
        if getattr(response, "status_code", 0) != 200:
            raise RuntimeError(f"claim failed: {getattr(response, 'status_code', '?')}")
        return response.json()

    def pull(self) -> list[dict[str, Any]]:
        delivered: list[dict[str, Any]] = []
        for row in self.list_inbox():
            claimed = self.claim(str(row["message_id"]))
            if claimed.get("outcome") in {"delivered", "replay_ignored"} and claimed.get("status") == "delivered":
                delivered.append(claimed)
        return delivered

    def acknowledge(self, message_id: str, *, event_id: str) -> dict[str, Any]:
        return self.submit_receipt(
            {"kind": "acknowledge", "event_id": event_id, "message_id": message_id}
        )

    def report_execution(self, message_id: str, *, event_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return self.submit_receipt(
            {
                "kind": "execution_progress",
                "event_id": event_id,
                "message_id": message_id,
                "evidence": evidence,
            }
        )

    def complete(self, message_id: str, *, event_id: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.submit_receipt(
            {
                "kind": "completion",
                "event_id": event_id,
                "message_id": message_id,
                "evidence": evidence or {"source": "local_authorized_bridge", "kind": "adapter_heartbeat"},
            }
        )

    def submit_receipt(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._call("POST", "/api/mainai/agent-gateway/receipts", json=payload)
        if getattr(response, "status_code", 0) != 200:
            raise RuntimeError(f"receipt failed: {getattr(response, 'status_code', '?')} {getattr(response, 'text', '')}")
        return response.json()


def main(argv: list[str] | None = None) -> int:
    del argv
    token = os.environ.get("LIFE_AGENT_BRIDGE_TOKEN", "").strip()
    base = os.environ.get("LIFE_AGENT_BRIDGE_BASE_URL", "http://127.0.0.1:8000").strip()
    if not token:
        print("BLOCKED: LIFE_AGENT_BRIDGE_TOKEN is missing", file=sys.stderr)
        return 2
    bridge = LocalAuthorizedBridge(token=token, base_url=base)
    try:
        delivered = bridge.pull()
        print(json.dumps({"delivered": len(delivered), "vendor_delivery_claimed": False, "messages": delivered}, indent=2))
        return 0
    finally:
        bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
