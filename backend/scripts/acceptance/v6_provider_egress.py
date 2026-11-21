#!/usr/bin/env python3
"""Provider-egress evidence for the V6 gate.

PASS requires both:
  * the running API reports a reachable local model with no missing models;
  * the backend container's effective environment has no paid-provider credentials and
    routes chat/embedding through Ollama only.

Missing information is BLOCKED, never PASS.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def _get(url: str) -> tuple[int | None, dict | None]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read()
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError:
                return response.status, None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--compose-file", default="docker-compose.testlab.yml")
    parser.add_argument("--project-name", default="lifeai-v6-testlab")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    checks: dict[str, dict] = {}
    status_code, health = _get(f"{args.base_url.rstrip('/')}/api/health")
    local = (health or {}).get("local_model") if isinstance(health, dict) else None
    checks["health"] = {"status": "PASS" if status_code == 200 else "BLOCKED", "http_status": status_code}
    checks["local_model"] = {
        "status": "PASS"
        if isinstance(local, dict) and local.get("reachable") is True and not local.get("missing_models")
        else "BLOCKED",
        "detail": local if isinstance(local, dict) else "no local_model block in /api/health",
    }

    env_result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            args.project_name,
            "-f",
            args.compose_file,
            "exec",
            "-T",
            "backend",
            "printenv",
        ],
        text=True,
        capture_output=True,
    )
    env = {}
    if env_result.returncode == 0:
        for line in env_result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env[key] = value
    else:
        checks["container_env"] = {"status": "BLOCKED", "detail": env_result.stderr[-400:]}
    if "container_env" not in checks:
        paid_keys = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "ZAI_API_KEY"]
        leaked = [key for key in paid_keys if env.get(key, "").strip()]
        fallback = env.get("CHAT_FALLBACK_ORDER", "")
        checks["container_env"] = {
            "status": "PASS" if not leaked and fallback == "ollama" else "FAIL",
            "paid_keys_present": leaked,
            "chat_fallback_order": fallback,
        }

    statuses = [check["status"] for check in checks.values()]
    status = "FAIL" if "FAIL" in statuses else ("BLOCKED" if "BLOCKED" in statuses else "PASS")
    payload = {"status": status, "checks": checks}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if status == "PASS" else (3 if status == "BLOCKED" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
