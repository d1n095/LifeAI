"""Least-privilege assignment executor.

Message text is never interpolated into a shell. The default `record` mode
persists structured evidence only. `allowlisted` runs a Founder-supplied argv
template with no assignment fields in the command.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from app.agent_coordination.adapter_config import real_adapter_config, resolve_adapter_env
from app.agent_coordination.durable import FIRST_CYCLE_GO

DEFAULT_TIMEOUT_SECONDS = 15


class RestrictedAssignmentExecutor:
    def run(self, message: dict[str, Any]) -> dict[str, Any]:
        mode = os.environ.get("LIFE_AGENT_SIDE_EXECUTOR", "record").strip().lower() or "record"
        payload = message.get("message") or message
        base = {
            "ran": False,
            "mode": mode,
            "write_scope": "none",
            "interpolated_instruction": False,
            "vendor_delivery_claimed": False,
            "first_cycle_go": FIRST_CYCLE_GO,
            "assignment_id": payload.get("obligation_id") or payload.get("assignment_id"),
            "message_id": payload.get("message_id"),
        }
        if mode == "record":
            base.update({"ran": True, "evidence_kind": "adapter_heartbeat", "summary": "assignment recorded; no subprocess"})
            return base
        if mode == "allowlisted":
            provider = os.environ.get("LIFE_AGENT_SIDE_PROVIDER", "").strip()
            config = real_adapter_config(provider) if provider else None
            if config is None:
                base.update(
                    {
                        "outcome": "blocked",
                        "missing_capability": "founder_adapter_enablement",
                        "reason": "allowlisted executor requires Founder-enabled provider + args template",
                    }
                )
                return base
            executable, args, timeout = config
            env = {"PATH": os.environ.get("PATH", "")}
            env.update(resolve_adapter_env(provider))
            completed = subprocess.run(
                [executable, *args],
                timeout=min(int(os.environ.get("LIFE_AGENT_SIDE_TIMEOUT_SECONDS") or timeout), DEFAULT_TIMEOUT_SECONDS),
                capture_output=True,
                env=env,
                check=False,
            )
            base.update(
                {
                    "ran": True,
                    "returncode": completed.returncode,
                    "evidence_kind": "adapter_heartbeat",
                    "provider": provider,
                }
            )
            return base
        base.update({"outcome": "blocked", "missing_capability": "founder_agent_side_authorization", "reason": f"unknown executor mode {mode}"})
        return base
