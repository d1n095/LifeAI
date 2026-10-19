"""Credential isolation attacks via real local subprocesses."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from first_cycle_acceptance._adapters import PRODUCTION
from first_cycle_acceptance._credentials import config_contains_token, probe_role, write_one_use_askpass
from first_cycle_acceptance._git import IsolatedRepo, git, seed_isolated_repo
from first_cycle_acceptance._grants import GrantStore
from first_cycle_acceptance._matrix import FAIL, PASS
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
    usable = "password=should-not-work" in probe.fill_stdout and "github.com" in probe.fill_stdout
    conclude(
        "CRED-4 credential helper cannot supply push authority",
        FAIL if usable else PASS,
        boundary="harness.credential_helper",
        detail=f"fill={probe.fill_stdout!r}; helper_cfg={probe.helper_stdout!r}",
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
