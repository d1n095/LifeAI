"""Transport contract probes against PRODUCTION git plumbing (worktree._run_git), network-free.

A localhost HTTP server answers every request with 401 Basic, which makes git consult its credential
helpers. The outer environment carries a CANARY global helper (run under a sandbox that denies the real
host helpers). Spec (codex/mainai-git-transport-credential-audit, credential_sources_to_exclude):
system/global/local helpers, askpass and env-injected config must never supply credentials to MainAI git.

  TC-1 production `_run_git` never invokes a global/system credential helper.
  TC-2 production `_run_git` ignores GIT_CONFIG_COUNT/KEY/VALUE helper injection.
  TC-3 production `_run_git` never invokes GIT_ASKPASS.
"""

from __future__ import annotations

import http.server
import os
import threading
from pathlib import Path

import pytest

MARKER = Path(os.environ["EXAMINER_CANARY_MARKER"])
ASKPASS_MARKER = Path(os.environ["EXAMINER_CANARY_MARKER"] + ".askpass")


class _Deny(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="examiner"')
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Deny)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/repo.git"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset():
    MARKER.unlink(missing_ok=True)
    ASKPASS_MARKER.unlink(missing_ok=True)


def _ls_remote(url, tmp_path):
    from app.mainai_execution import worktree

    try:
        worktree._run_git(["ls-remote", url], cwd=tmp_path, timeout=20)
    except Exception:  # noqa: BLE001  (auth failure expected; we only care who was asked)
        pass


def test_TC0_control_plain_git_reaches_canary(server, tmp_path):
    import subprocess

    subprocess.run(["git", "ls-remote", server], cwd=tmp_path, capture_output=True, text=True, timeout=20,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    assert MARKER.exists(), "control failed: canary helper not reachable (test would be vacuous)"


def test_TC1_production_git_never_consults_host_helpers(server, tmp_path):
    _ls_remote(server, tmp_path)
    assert not MARKER.exists(), "TC-1 production _run_git consulted a global/system credential helper"


def test_TC2_env_injected_helper_ignored(server, tmp_path, monkeypatch):
    helper = os.environ["EXAMINER_CANARY_HELPER"]
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"!{helper}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    _ls_remote(server, tmp_path)
    assert not MARKER.exists(), "TC-2 env-injected credential.helper reached production git"


def test_TC3_askpass_never_invoked(server, tmp_path, monkeypatch):
    askpass = tmp_path / "askpass.sh"
    askpass.write_text(f"#!/bin/sh\ntouch '{ASKPASS_MARKER}'\necho canary\n")
    askpass.chmod(0o755)
    monkeypatch.setenv("GIT_ASKPASS", str(askpass))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    _ls_remote(server, tmp_path)
    assert not ASKPASS_MARKER.exists(), "TC-3 production git invoked GIT_ASKPASS"
