"""#286 P1-3 verification WITHOUT touching host credential helpers.

The outer process is given a *canary* global git config whose credential helper prints a fake
password and records every invocation. Run inside a macOS sandbox profile that denies reads of
the real ~/.gitconfig, gh config, keychain, and exec of gh / git-credential-osxkeychain.
A control call proves the canary would leak without isolation; the harness's isolated_git_env
must never invoke it.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

CANARY = os.environ["EXAMINER_CANARY_SECRET"]
MARKER = Path(os.environ["EXAMINER_CANARY_MARKER"])
FILL_INPUT = "protocol=https\nhost=github.com\n\n"


def _fill(env):
    return subprocess.run(["git", "credential", "fill"], input=FILL_INPUT, capture_output=True, text=True, env=env, timeout=10)


@pytest.fixture(autouse=True)
def _reset_marker():
    MARKER.unlink(missing_ok=True)
    yield


def test_C0_control_canary_leaks_without_isolation():
    out = _fill({**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    assert CANARY in out.stdout and MARKER.exists(), "control failed: canary helper not reachable, test would be vacuous"


def test_C1_isolated_env_never_invokes_outer_helper():
    from first_cycle_acceptance._git import isolated_git_env

    out = _fill(isolated_git_env(os.environ))
    assert CANARY not in out.stdout + out.stderr and not MARKER.exists()


def test_C2_extra_env_cannot_repoint_git_at_host_config():
    from first_cycle_acceptance._git import isolated_git_env

    outer_global = os.environ["GIT_CONFIG_GLOBAL"]
    env = isolated_git_env(os.environ, extra={"HOME": os.environ["HOME"], "GIT_CONFIG_GLOBAL": outer_global, "XDG_CONFIG_HOME": "/tmp"})
    out = _fill(env)
    assert CANARY not in out.stdout and not MARKER.exists()


def test_C3_env_injected_helper_is_stripped():
    from first_cycle_acceptance._git import isolated_git_env

    helper = os.environ["EXAMINER_CANARY_HELPER"]
    base = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": f"!{helper}",
            "GIT_CONFIG_PARAMETERS": f"'credential.helper'='!{helper}'"}
    out = _fill(isolated_git_env(base))
    assert CANARY not in out.stdout and not MARKER.exists()


def test_C4_extra_env_injected_helper_via_config_count():
    """A caller-supplied extra env must not re-enable a helper via GIT_CONFIG_COUNT/KEY/VALUE."""
    from first_cycle_acceptance._git import isolated_git_env

    helper = os.environ["EXAMINER_CANARY_HELPER"]
    out = _fill(isolated_git_env(os.environ, extra={"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": f"!{helper}"}))
    assert CANARY not in out.stdout and not MARKER.exists(), "extra env re-enabled a credential helper"


def test_C5_matrix_sanitizer_and_no_repo_copy(tmp_path, monkeypatch):
    from first_cycle_acceptance import _matrix

    detail = f"fill='password={CANARY}\\n' token gho_{'A' * 36} ghp_{'B' * 36} github_pat_{'C' * 40}"
    clean = _matrix.sanitize_matrix_text(detail)
    assert CANARY not in clean and "gho_A" not in clean and "ghp_B" not in clean and "github_pat_C" not in clean
    monkeypatch.delenv("FIRST_CYCLE_ACCEPTANCE_WRITE_REPO_COPY", raising=False)
    repo_copy = Path(_matrix.__file__).resolve().parents[4] / "docs" / "examiner" / "first_cycle_acceptance_matrix.json"
    existed = repo_copy.exists()
    _matrix.write_payload(tmp_path / "m.json")
    assert repo_copy.exists() == existed and (tmp_path / "m.json").exists()
