"""A — External publication / grant / base failures, injected at their REAL origin.

Criteria FO-1..FO-5 (ACCEPTANCE.md). For every failure in the catalog, after the real worker has
run every tick it would run (retry backoff, lease expiry and wait polls made due):
  FO-1  the repo_edit task is terminal `failed` (never retryable_failed / ready / running);
  FO-2  ZERO additional paid calls: code-agent calls <= the single first attempt, planner calls == 1;
  FO-3  exactly one execution job was ever dispatched for the task (no retry / recovery re-run);
  FO-4  the dependent open_pr was never dispatched;
  FO-5  classification does not depend on message text: an OPAQUE failure raised at the
        publication boundary is treated exactly like a recognised one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import get_settings
from app.integrations.github_client import GitHubClientError
from app.mainai_execution import execution_job
from app.mainai_execution import worktree as worktree_module
from app.mainai_execution.worktree import WorktreeError
from app.models.mainai_execution import MainAITaskStatus

from _harness import (  # noqa: F401
    ACTIVATION_BASE, PATHS, _privileges, _production_spend_gate, drive, edit_payload, git, install_github, install_provider,
    jobs_for_task, owner_id, plan_payload, planned_goal, seed_remote, task_branches, trail,
)

REAL = json.loads((Path(__file__).parent / "real_git_errors.json").read_text())
STDERR_403 = ("remote: Permission to test-owner/test-repo.git denied to mainai-runtime.\n"
              "fatal: unable to access 'https://github.com/test-owner/test-repo.git/': The requested URL returned error: 403")
STDERR_NETWORK = "fatal: unable to access 'https://github.com/test-owner/test-repo.git/': Could not resolve host: github.com"

# Verbatim error texts raised by Codex's #280 grant substrate (activation_security.py @ 37bca08).
GRANT_DENIALS = {
    "grant_expired": "push grant expired",
    "grant_consumed": "push grant was already consumed",
    "grant_base_moved": "base branch moved after approval",
    "grant_head_mismatch": "authoritative worktree HEAD no longer matches the grant",
    "grant_envelope_superseded": "execution envelope was superseded after grant creation",
    "grant_receipt_stale": "preflight receipt is stale",
    "grant_version_mismatch": "authority version mismatch",
}


# --------------------------------------------------------------------------- injections
def _wrap_push(monkeypatch, before):
    """Run `before(worktree)` immediately before the real push_worktree_branch (publication boundary)."""
    original = execution_job.push_worktree_branch

    async def wrapped(worktree):
        before(worktree)
        return await original(worktree)

    monkeypatch.setattr(execution_job, "push_worktree_branch", wrapped)


def _fake_git_push(monkeypatch, behaviour):
    """Replace ONLY the `git push` subprocess with a scripted outcome; real _run_git formats it."""
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "push"]:
            return behaviour(cmd, kwargs)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_module.subprocess, "run", run)


def inject(name, monkeypatch, gh, tmp_path, task_ids):
    if name == "git_pre_receive_declined":  # real git: remote hook refuses
        hook = gh.bare / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'push not authorized' >&2\nexit 1\n")
        hook.chmod(0o755)
    elif name == "git_non_fast_forward":  # real git: remote task branch already diverged
        seed = tmp_path / "seed"
        branch = f"claude/mainai-task-{task_ids['repo_edit']}"
        git("checkout", "-q", "-b", branch, cwd=seed)
        (seed / "diverged.txt").write_text("someone else\n")
        git("add", "-A", cwd=seed)
        git("commit", "-q", "-m", "diverged", cwd=seed)
        git("push", "-q", "origin", branch, cwd=seed)
    elif name == "git_remote_unreachable":  # real git: the push endpoint is unreachable (fetch/REST unaffected)
        missing = str(tmp_path / "unreachable-remote.git")

        def unreachable(worktree):
            git("remote", "set-url", "--push", "origin", missing, cwd=worktree.path)
        _wrap_push(monkeypatch, unreachable)
    elif name == "git_push_timeout":
        def timeout(cmd, kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 120))
        _fake_git_push(monkeypatch, timeout)
    elif name == "git_https_403":
        _fake_git_push(monkeypatch, lambda cmd, kw: subprocess.CompletedProcess(cmd, 128, "", STDERR_403))
    elif name == "git_network":
        _fake_git_push(monkeypatch, lambda cmd, kw: subprocess.CompletedProcess(cmd, 128, "", STDERR_NETWORK))
    elif name == "post_push_remote_mismatch":  # real push succeeds; GitHub reports another tip
        gh.get_ref_override = lambda b: "e" * 40 if b.startswith("claude/mainai-task-") else None
    elif name == "github_get_ref_404":
        def raise_404(b):
            if b.startswith("claude/mainai-task-"):
                raise GitHubClientError(f"GitHub GET /repos/test-owner/test-repo/git/ref/heads/{b} misslyckades (404): "
                                        '{"message":"Not Found"}', status_code=404)
            return None
        gh.get_ref_override = raise_404
    elif name in GRANT_DENIALS:  # transport/grant refuses at the publication boundary, before any remote effect
        message = GRANT_DENIALS[name]
        _wrap_push(monkeypatch, lambda wt: (_ for _ in ()).throw(WorktreeError(message)))
    elif name == "opaque_boundary_failure":  # FO-5: no recognisable words at all
        _wrap_push(monkeypatch, lambda wt: (_ for _ in ()).throw(WorktreeError("zq-7f3 opaque transport status 0x2a")))
    elif name == "untyped_boundary_exception":  # e.g. #280's ActivationAuthorityError is a RuntimeError
        _wrap_push(monkeypatch, lambda wt: (_ for _ in ()).throw(RuntimeError("authoritative worktree HEAD no longer matches the grant")))
    else:
        raise AssertionError(name)


CATALOG = [
    "git_pre_receive_declined", "git_non_fast_forward", "git_remote_unreachable", "git_push_timeout", "git_https_403",
    "git_network", "post_push_remote_mismatch", "github_get_ref_404", *GRANT_DENIALS, "opaque_boundary_failure",
    "untyped_boundary_exception",
]


def _check_contained(db, goal, paid, gh, *, label, max_code_agent=1):
    tasks = __import__("_harness").tasks_by_type(db, goal.id)
    edit = tasks["repo_edit"][0]
    pr = tasks.get("open_pr", [None])[0]
    code_agent = paid.kinds("code_agent")
    planner_calls = paid.kinds("planner")
    jobs = jobs_for_task(db, edit.id)
    pr_jobs = jobs_for_task(db, pr.id) if pr is not None else 0
    detail = (f"{label}: status={edit.status.value} next_retry_at={edit.next_retry_at} jobs={jobs} code_agent_calls={len(code_agent)} "
              f"planner_calls={len(planner_calls)} open_pr_jobs={pr_jobs} trail={trail(db, goal.id)[-4:]}")
    assert edit.status == MainAITaskStatus.failed and edit.next_retry_at is None, "FO-1 " + detail
    assert len(code_agent) <= max_code_agent and len(planner_calls) == 1, "FO-2 " + detail
    assert jobs == 1, "FO-3 " + detail
    assert pr_jobs == 0, "FO-4 " + detail


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CATALOG)
async def test_FO_external_failure_is_terminal_and_never_paid_again(db_session, superuser_db, owner_id, monkeypatch, tmp_path, name):
    bare = seed_remote(tmp_path, "integration/founder-alpha-test-base")
    gh = install_github(monkeypatch, bare, "integration/founder-alpha-test-base")
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda user: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    ids = {k: v[0].id for k, v in __import__("_harness").tasks_by_type(db_session, goal.id).items()}
    inject(name, monkeypatch, gh, tmp_path, ids)
    try:
        await drive(superuser_db, goal.id)
    finally:
        gone = Path(str(bare) + ".gone")
        if gone.exists():
            shutil.move(str(gone), str(bare))
    _check_contained(superuser_db, goal, paid, gh, label=name)


@pytest.mark.asyncio
async def test_FO_approved_base_moved_is_terminal_and_never_paid(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """Activation mode: integration tip != Founder-approved SHA (real create_task_worktree check)."""
    bare = seed_remote(tmp_path, ACTIVATION_BASE)
    install_github(monkeypatch, bare, ACTIVATION_BASE)
    settings = get_settings()
    monkeypatch.setattr(settings, "mainai_activation_mode", True)
    monkeypatch.setattr(settings, "mainai_activation_approved_base_sha", "f" * 40)
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda user: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    await drive(superuser_db, goal.id)
    _check_contained(superuser_db, goal, paid, None, label="approved_base_moved", max_code_agent=0)
