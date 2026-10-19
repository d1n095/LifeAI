"""Credential isolation attacks via real local subprocesses."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from first_cycle_acceptance._adapters import PRODUCTION
from first_cycle_acceptance._credentials import config_contains_token, probe_role, write_one_use_askpass
from first_cycle_acceptance._git import IsolatedRepo, git, isolated_git_env, seed_isolated_repo
from first_cycle_acceptance._grants import GrantStore
from first_cycle_acceptance._matrix import FAIL, PASS, sanitize_matrix_text
from first_cycle_acceptance.conftest import REMAINING_OUT_OF_SCOPE, conclude

pytestmark = pytest.mark.first_cycle_acceptance

TOKEN = "test-token-must-not-leak-into-worktree"


@REMAINING_OUT_OF_SCOPE
def test_production_authed_remote_url_embeds_token():
    fn = PRODUCTION["worktree"].symbols.get("_authed_remote_url")
    if fn is None:
        conclude(
            "CRED-1 production remote URL constructor",
            FAIL,
            boundary="production._authed_remote_url",
            detail="symbol missing",
            production_module="app.mainai_execution.worktree",
        )
        return
    url = fn("test-owner/test-repo", TOKEN)
    embeds = TOKEN in url
    src = inspect.getsource(fn)
    conclude(
        "CRED-1 production does not embed the token in the remote URL",
        FAIL if embeds else PASS,
        boundary="production._authed_remote_url",
        detail=f"url_contains_token={embeds}; source={src.strip()!r}",
        production_module="app.mainai_execution.worktree",
    )


@REMAINING_OUT_OF_SCOPE
def test_worktree_config_must_not_contain_token(tmp_path):
    repo = seed_isolated_repo(tmp_path / "iso")
    # Simulate what production create_task_worktree writes today.
    fn = PRODUCTION["worktree"].symbols.get("_authed_remote_url")
    assert fn is not None
    authed = fn("test-owner/test-repo", TOKEN)
    git("remote", "set-url", "origin", authed, cwd=repo.work)
    config = (repo.work / ".git" / "config").read_text(encoding="utf-8")
    leaked = config_contains_token(config, TOKEN)
    conclude(
        "CRED-2 worktree .git/config contains no token after production URL write",
        FAIL if leaked else PASS,
        boundary="production.create_task_worktree.remote",
        detail=f"leaked={leaked}; origin={authed}",
        production_module="app.mainai_execution.worktree",
    )


@REMAINING_OUT_OF_SCOPE
def test_worker_test_reviewer_subprocesses_cannot_read_writable_credentials(tmp_path):
    repo = seed_isolated_repo(tmp_path / "iso")
    fn = PRODUCTION["worktree"].symbols.get("_authed_remote_url")
    git("remote", "set-url", "origin", fn("test-owner/test-repo", TOKEN), cwd=repo.work)
    leaks = {}
    for role in ("worker", "test", "reviewer"):
        probe = probe_role(role, repo.work)
        leaks[role] = probe.leaked_token(TOKEN) or config_contains_token(probe.config, TOKEN)
    any_leak = any(leaks.values())
    conclude(
        "CRED-3 worker/test/reviewer subprocesses cannot read writable remote credentials",
        FAIL if any_leak else PASS,
        boundary="harness.subprocess_probe",
        detail=f"leaks={leaks}",
        production_module="app.mainai_execution.worktree",
    )


def test_credential_helper_cannot_supply_authority(isolated_repo):
    helper = isolated_repo.grant_box / "evil-helper.sh"
    helper.write_text("#!/bin/sh\necho password=should-not-work\n", encoding="utf-8")
    helper.chmod(0o755)
    git("config", "--local", "credential.helper", str(helper), cwd=isolated_repo.work)
    probe = probe_role("worker", isolated_repo.work, extra_env={"GIT_TERMINAL_PROMPT": "0"})
    # Helper output in a worker probe must not be treated as authority, and fill must not
    # produce a usable password for github.com from worktree config (credential-free origin).
    # Compare only a digest — never record the password itself.
    helper_digest = __import__("hashlib").sha256(b"should-not-work").hexdigest()
    usable = probe.fill_password_digest == helper_digest and probe.fill_mentions_github
    conclude(
        "CRED-4 credential helper cannot supply push authority",
        FAIL if usable else PASS,
        boundary="harness.credential_helper",
        detail=(
            f"fill_had_password={probe.fill_had_password}; "
            f"fill_mentions_github={probe.fill_mentions_github}; "
            f"helper_cfg={probe.helper_stdout!r}"
        ),
    )


def test_git_environment_injection_cannot_supply_authority(isolated_repo):
    probe = probe_role(
        "worker",
        isolated_repo.work,
        extra_env={"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN, "GIT_ASKPASS": "/bin/echo"},
    )
    # The probe function strips those keys before launch unless we pass extra_env which
    # re-adds them — this models a compromised parent injecting env. The worktree origin
    # is a filesystem path, so env tokens must still not appear in .git/config.
    config_leak = config_contains_token(probe.config, TOKEN)
    conclude(
        "CRED-5 Git environment injection does not persist into .git/config",
        FAIL if config_leak else PASS,
        boundary="harness.env_injection",
        detail=f"config_leak={config_leak}; env_hits={probe.env_hits}",
    )


def test_one_use_executor_credential_dies_on_cancel_and_recovery(isolated_repo):
    askpass = write_one_use_askpass(isolated_repo.grant_box / "askpass", TOKEN)
    token_file = Path(str(askpass) + ".token")
    assert token_file.is_file()
    store = GrantStore()
    grant = store.issue(
        kind="push",
        owner_id="o",
        goal_id="g",
        task_id="t",
        job_id="j",
        worktree_id="w",
        repository="test-owner/test-repo",
        branch="x",
        base_sha=isolated_repo.base_sha,
        commit_sha="0" * 40,
    )
    store.cancel(grant.id)
    if token_file.exists():
        token_file.unlink()
        askpass.unlink(missing_ok=True)
    revived = token_file.is_file()
    # Recovery must not recreate the token.
    conclude(
        "CRED-6 cancellation/recovery cannot revive credentials",
        PASS if (not revived and store.get(grant.id).cancelled) else FAIL,
        boundary="harness.one_use_credential",
        detail=f"token_file_exists={revived}; cancelled={store.get(grant.id).cancelled}",
    )


def test_direct_ungranted_push_refused(isolated_repo):
    git("checkout", "-q", "-B", "claude/mainai-task-direct", isolated_repo.base_sha, cwd=isolated_repo.work)
    (isolated_repo.work / "backend" / "app" / "greeting.py").write_text("def greet(name):\n    return 'x'\n")
    git("add", "-A", cwd=isolated_repo.work)
    git("commit", "-q", "-m", "direct", cwd=isolated_repo.work)
    result = git(
        "push",
        "origin",
        "HEAD:claude/mainai-task-direct",
        cwd=isolated_repo.work,
        check=False,
    )
    refused = result.returncode != 0
    conclude(
        "CRED-7 direct ungranted push is refused",
        PASS if refused else FAIL,
        boundary="harness.remote_grant_hook",
        detail=f"returncode={result.returncode}; stderr={(result.stderr or '')[:400]}",
    )
    assert refused


def test_ordinary_pytest_cannot_access_host_git_credentials(tmp_path, monkeypatch, isolated_repo):
    """Regression for P1-3: a developer-machine credential helper must be invisible to the
    harness. The secret is synthetic; this test never reads a real host token."""
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    helper = tmp_path / "host-helper.sh"
    secret = "SYNTHETIC_HOST_HELPER_PASSWORD_NOT_A_TOKEN"
    helper.write_text(f"#!/bin/sh\necho password={secret}\n", encoding="utf-8")
    helper.chmod(0o755)
    host_config = host_home / ".gitconfig"
    host_config.write_text(f"[credential]\n\thelper = {helper}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(host_config))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)

    env = isolated_git_env()
    assert Path(env["GIT_CONFIG_GLOBAL"]).resolve() != host_config.resolve()
    assert Path(env["HOME"]).resolve() != host_home.resolve()
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert secret not in Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")

    probe = probe_role("worker", isolated_repo.work)
    leaked = (
        secret in json.dumps(probe.raw)
        or secret in probe.config
        or secret in probe.helper_stdout
        or (probe.fill_password_digest == hashlib.sha256(secret.encode()).hexdigest())
    )
    detail = (
        f"leaked={leaked}; fill_had_password={probe.fill_had_password}; "
        f"isolated_home={probe.isolated_home!r}; "
        f"isolated_global={probe.isolated_global_config!r}"
    )
    assert secret not in detail
    conclude(
        "CRED-8 ordinary pytest cannot access host git credentials",
        FAIL if leaked else PASS,
        boundary="harness.host_credential_isolation",
        detail=detail,
    )
    assert not leaked
    assert sanitize_matrix_text(f"password={secret}") == "password=<redacted>"


def test_extra_env_cannot_reinject_git_config(tmp_path, monkeypatch, isolated_repo):
    """P1: extra_env may model a compromised parent token, but GIT_CONFIG_COUNT / KEY / VALUE
    / PARAMETERS must not re-open host git config after isolation."""
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    helper = tmp_path / "injected-helper.sh"
    secret = "SYNTHETIC_EXTRA_ENV_GIT_CONFIG_PASSWORD"
    helper.write_text(f"#!/bin/sh\necho password={secret}\n", encoding="utf-8")
    helper.chmod(0o755)
    injected_config = tmp_path / "injected.gitconfig"
    injected_config.write_text(f"[credential]\n\thelper = {helper}\n", encoding="utf-8")

    env = isolated_git_env(
        extra={
            "GITHUB_TOKEN": "gho_synthetic_extra_env_token_not_real",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": str(helper),
            "GIT_CONFIG_PARAMETERS": f"'credential.helper={helper}'",
            "GIT_CONFIG": str(injected_config),
            "HOME": str(host_home),
            "GIT_CONFIG_GLOBAL": str(injected_config),
        }
    )
    injected = (
        env.get("GIT_CONFIG_COUNT")
        or env.get("GIT_CONFIG_KEY_0")
        or env.get("GIT_CONFIG_VALUE_0")
        or env.get("GIT_CONFIG_PARAMETERS")
        or env.get("GIT_CONFIG")
    )
    assert injected is None
    assert Path(env["HOME"]).resolve() != host_home.resolve()
    assert Path(env["GIT_CONFIG_GLOBAL"]).resolve() != injected_config.resolve()
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert env.get("GITHUB_TOKEN") == "gho_synthetic_extra_env_token_not_real"

    probe = probe_role(
        "worker",
        isolated_repo.work,
        extra_env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": str(helper),
            "GIT_CONFIG_PARAMETERS": f"'credential.helper={helper}'",
            "GIT_CONFIG": str(injected_config),
        },
    )
    leaked = (
        secret in json.dumps(probe.raw)
        or secret in probe.config
        or secret in probe.helper_stdout
        or (probe.fill_password_digest == hashlib.sha256(secret.encode()).hexdigest())
    )
    conclude(
        "CRED-9 extra_env cannot re-inject GIT_CONFIG",
        FAIL if leaked or injected else PASS,
        boundary="harness.git_config_injection",
        detail=f"leaked={leaked}; injected_keys_present={bool(injected)}",
    )
    assert not leaked
