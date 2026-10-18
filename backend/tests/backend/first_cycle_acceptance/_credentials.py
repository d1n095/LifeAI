"""Credential isolation probes that run in real local subprocesses.

The simulated effect executor may receive a one-use test credential. Worker, test and
reviewer processes, and the worktree `.git/config`, must not.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

PROBE_SCRIPT = textwrap.dedent(
    """
    import json, os, pathlib, subprocess, sys
    root = pathlib.Path(os.environ["FIRST_CYCLE_PROBE_ROOT"])
    config = ""
    config_path = root / ".git" / "config"
    if config_path.is_file():
        config = config_path.read_text(encoding="utf-8", errors="replace")
    env_hits = {k: v for k, v in os.environ.items() if k in {
        "GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS", "GIT_USERNAME", "GIT_PASSWORD",
        "MAINAI_PUSH_TOKEN", "MAINAI_EFFECT_TOKEN",
    } and v}
    helper = subprocess.run(
        ["git", "-c", "credential.helper=", "config", "--get-regexp", "credential"],
        cwd=str(root), capture_output=True, text=True,
    )
    fill = subprocess.run(
        ["git", "credential", "fill"],
        cwd=str(root),
        input="protocol=https\\nhost=github.com\\npath=test-owner/test-repo.git\\n\\n",
        capture_output=True, text=True, timeout=3,
    )
    print(json.dumps({
        "config": config,
        "env_hits": env_hits,
        "helper_stdout": helper.stdout,
        "helper_stderr": helper.stderr,
        "fill_stdout": fill.stdout,
        "fill_returncode": fill.returncode,
        "cwd": os.getcwd(),
        "role": os.environ.get("FIRST_CYCLE_PROBE_ROLE", "unknown"),
    }))
    """
)


@dataclass
class ProbeResult:
    role: str
    config: str
    env_hits: dict
    helper_stdout: str
    fill_stdout: str
    raw: dict

    def leaked_token(self, token: str) -> bool:
        blob = json.dumps(self.raw)
        return bool(token) and token in blob


def probe_role(role: str, worktree: Path, *, extra_env: dict | None = None) -> ProbeResult:
    env = {**os.environ, **(extra_env or {})}
    env["FIRST_CYCLE_PROBE_ROOT"] = str(worktree)
    env["FIRST_CYCLE_PROBE_ROLE"] = role
    # Worker/test/reviewer processes must not receive writable remote credentials.
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "MAINAI_PUSH_TOKEN", "MAINAI_EFFECT_TOKEN", "GIT_ASKPASS"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-c", PROBE_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd="/tmp",
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"probe {role} failed: {result.stderr[:800]}")
    payload = json.loads(result.stdout)
    return ProbeResult(
        role=role,
        config=payload.get("config") or "",
        env_hits=payload.get("env_hits") or {},
        helper_stdout=payload.get("helper_stdout") or "",
        fill_stdout=payload.get("fill_stdout") or "",
        raw=payload,
    )


def write_one_use_askpass(path: Path, token: str) -> Path:
    """Askpass helper that yields a token once, then self-destructs."""
    path.write_text(
        f"""#!/bin/sh
token_file="{path}.token"
if [ ! -f "$token_file" ]; then
  echo "one-use credential already consumed" >&2
  exit 1
fi
cat "$token_file"
rm -f "$token_file"
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    (path.parent / (path.name + ".token")).write_text(token, encoding="utf-8")
    return path


def config_contains_token(config_text: str, token: str) -> bool:
    if token and token in config_text:
        return True
    lowered = config_text.lower()
    return "x-access-token:" in lowered or "://ghp_" in lowered or "://github_pat_" in lowered
