"""Credential isolation probes that run in real local subprocesses.

The simulated effect executor may receive a one-use test credential. Worker, test and
reviewer processes, and the worktree `.git/config`, must not.

Every probe uses isolated temporary Git configuration. Host credential helpers and personal
tokens are never inherited. Raw `git credential fill` output is never retained — only
booleans and a SHA-256 digest of any password line.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from first_cycle_acceptance._git import isolated_git_env

_PASSWORD_LINE = re.compile(r"(?im)^password=(.*)$")

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
        ["git", "config", "--get-regexp", "credential"],
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
        "env_hits": {k: "<set>" for k in env_hits},
        "helper_stdout": helper.stdout,
        "helper_stderr": helper.stderr,
        "fill_stdout": fill.stdout,
        "fill_returncode": fill.returncode,
        "cwd": os.getcwd(),
        "role": os.environ.get("FIRST_CYCLE_PROBE_ROLE", "unknown"),
        "home": os.environ.get("HOME"),
        "git_config_global": os.environ.get("GIT_CONFIG_GLOBAL"),
        "git_config_nosystem": os.environ.get("GIT_CONFIG_NOSYSTEM"),
    }))
    """
)


def _password_digest(fill_stdout: str) -> str | None:
    match = _PASSWORD_LINE.search(fill_stdout or "")
    if match is None or not match.group(1):
        return None
    return hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()


def redact_credential_text(text: str) -> str:
    if not text:
        return ""
    redacted = _PASSWORD_LINE.sub("password=<redacted>", text)
    redacted = re.sub(r"(?im)^(username|authorization)=.*$", lambda m: f"{m.group(1).lower()}=<redacted>", redacted)
    return redacted


@dataclass
class ProbeResult:
    role: str
    config: str
    env_hits: dict
    helper_stdout: str
    fill_had_password: bool
    fill_mentions_github: bool
    fill_password_digest: str | None
    fill_returncode: int
    isolated_home: str
    isolated_global_config: str
    raw: dict

    def leaked_token(self, token: str) -> bool:
        if not token:
            return False
        blob = json.dumps(self.raw)
        return token in blob or token in self.config or token in self.helper_stdout


def probe_role(role: str, worktree: Path, *, extra_env: dict | None = None) -> ProbeResult:
    env = isolated_git_env(os.environ, extra=extra_env)
    env["FIRST_CYCLE_PROBE_ROOT"] = str(worktree)
    env["FIRST_CYCLE_PROBE_ROLE"] = role
    # Worker/test/reviewer processes must not receive writable remote credentials unless the
    # caller is explicitly modelling a compromised parent (extra_env).
    if not extra_env:
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "MAINAI_PUSH_TOKEN", "MAINAI_EFFECT_TOKEN", "GIT_ASKPASS"):
            env.pop(key, None)
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
        raise RuntimeError(f"probe {role} failed: {(result.stderr or result.stdout)[:800]}")
    payload = json.loads(result.stdout)
    raw_fill = payload.get("fill_stdout") or ""
    digest = _password_digest(raw_fill)
    safe_payload = dict(payload)
    safe_payload["fill_stdout"] = redact_credential_text(raw_fill)
    safe_payload["fill_had_password"] = bool(digest)
    safe_payload["fill_password_digest"] = digest
    return ProbeResult(
        role=role,
        config=payload.get("config") or "",
        env_hits=payload.get("env_hits") or {},
        helper_stdout=redact_credential_text(payload.get("helper_stdout") or ""),
        fill_had_password=bool(digest),
        fill_mentions_github="github.com" in raw_fill,
        fill_password_digest=digest,
        fill_returncode=int(payload.get("fill_returncode") or 0),
        isolated_home=str(payload.get("home") or ""),
        isolated_global_config=str(payload.get("git_config_global") or ""),
        raw=safe_payload,
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
