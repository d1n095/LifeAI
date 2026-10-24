"""Agent-side receiver entry.

Run after the Founder issues a scoped gateway token:

    LIFE_AGENT_BRIDGE_TOKEN=... python -m app.agent_coordination.agent_side

This process is the authenticated receiving agent. It is not Cursor/Claude/Codex
vendor messaging and does not open a private vendor session.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from app.agent_coordination.durable import FIRST_CYCLE_GO
from app.agent_coordination.transport import MISSING_RUNNER_AUTH, fail_closed
from app.agent_coordination.transports.gateway import AuthorizedGatewayTransport


def run_agent_side(
    *,
    token: str | None = None,
    request: Callable[..., Any] | None = None,
    execute: bool = True,
) -> dict[str, Any]:
    token = (token or os.environ.get("LIFE_AGENT_BRIDGE_TOKEN") or "").strip()
    if not token:
        return fail_closed(
            transport_id="authorized_gateway_runner",
            missing_capability=MISSING_RUNNER_AUTH,
            reason="LIFE_AGENT_BRIDGE_TOKEN is required",
        )
    transport = AuthorizedGatewayTransport(token=token, request=request)
    try:
        return transport.receive_cycle(execute=execute)
    finally:
        transport.close()


def main(argv: list[str] | None = None) -> int:
    del argv
    result = run_agent_side()
    print(json.dumps(result, indent=2))
    if result.get("outcome") == "blocked":
        print(f"BLOCKED: {result.get('reason')}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
