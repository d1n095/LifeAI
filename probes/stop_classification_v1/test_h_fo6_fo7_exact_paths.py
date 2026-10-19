"""H — FO-6 / FO-7 on their EXACT original production paths, side by side with the neighbouring
paths #290 hardened (criteria FO-7x, FO-7n, FO-6x). Deterministic; real production functions;
the only injection is the outcome of one `git` subprocess (network failure) or one boundary call.

  FO-7x (exact, required for closure): `create_task_worktree_sync` — the approved-base
        `git fetch --depth 1 origin <base>` — must surface a network failure as a typed
        fail-closed `ExternalBoundaryError` (not a plain local `WorktreeError`).
  FO-7n (neighbour, fixed by #290): `fetch_remote_branch` — the recovery/operator fetch — must
        surface the same failure as typed (and never as "no remote branch").
  FO-6x (exact): a bare `RuntimeError` raised at the push boundary must end terminal, not leave
        the task `running`. The recorded takeover count shows why #290's one-takeover cap does
        not engage on this path (no takeover is ever attempted).
"""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy import text

from app.mainai_execution import execution_job
from app.mainai_execution import worktree as worktree_module
from app.mainai_execution.executor import dispatch_ready_task
from app.models.mainai_execution import MainAIGoal, MainAITaskStatus

from _harness import (  # noqa: F401
    LEGACY_BASE, PATHS, _privileges, _production_spend_gate, drive, edit_payload, git, github, install_provider, jobs_for_task,
    owner_id, plan_payload, planned_goal, tasks_by_type, trail,
)

STDERR_NETWORK = "fatal: unable to access 'https://github.com/test-owner/test-repo.git/': Could not resolve host: github.com"


def _boundary_types():
    try:
        from app.mainai_execution.publication_errors import ExternalBoundaryError
    except ImportError:  # pre-#288 lineages
        return ()
    return (ExternalBoundaryError,)


def _fail_git(monkeypatch, *prefixes):
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any(cmd[: len(p)] == list(p) for p in prefixes):
            return subprocess.CompletedProcess(cmd, 128, "", STDERR_NETWORK)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_module.subprocess, "run", run)


async def _dispatched_repo_edit(db, owner_id, monkeypatch):
    install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db, owner_id, paths=PATHS)
    task = tasks_by_type(db, goal.id)["repo_edit"][0]
    job = dispatch_ready_task(db, task=task, goal=db.get(MainAIGoal, goal.id), dispatched_by="examiner")
    db.commit()
    return goal, task, job


@pytest.mark.asyncio
async def test_FO7x_exact_path_base_fetch_failure_is_typed_boundary_error(db_session, owner_id, monkeypatch, github):
    goal, task, job = await _dispatched_repo_edit(db_session, owner_id, monkeypatch)
    base_sha = git("-C", str(github.bare), "rev-parse", f"refs/heads/{LEGACY_BASE}")
    _fail_git(monkeypatch, ("git", "fetch", "--depth"))
    with pytest.raises(Exception) as caught:
        worktree_module.create_task_worktree_sync(db_session, task=task, job=job, lease_generation=job.lease_generation or 1,
                                                  executor_id="examiner", base_sha=base_sha)
    db_session.rollback()
    exc = caught.value
    typed = _boundary_types()
    assert typed and isinstance(exc, typed), (
        f"FO-7x NOT CLOSED: create_task_worktree_sync base-fetch network failure raises {type(exc).__module__}.{type(exc).__name__} "
        f"(MRO={[c.__name__ for c in type(exc).__mro__[:4]]}), not an ExternalBoundaryError -> treated as local -> retries + paid replan"
    )


@pytest.mark.asyncio
async def test_FO7n_neighbour_recovery_fetch_failure_is_typed_not_missing_branch(db_session, owner_id, monkeypatch, github):
    goal, task, job = await _dispatched_repo_edit(db_session, owner_id, monkeypatch)
    base_sha = git("-C", str(github.bare), "rev-parse", f"refs/heads/{LEGACY_BASE}")
    wt = worktree_module.create_task_worktree_sync(db_session, task=task, job=job, lease_generation=job.lease_generation or 1,
                                                   executor_id="examiner", base_sha=base_sha)
    db_session.commit()
    _fail_git(monkeypatch, ("git", "fetch"), ("git", "ls-remote"), ("git", "-C"))
    try:
        result = worktree_module.fetch_remote_branch(wt)
        outcome = f"returned {result!r}"
        typed_ok = False
    except Exception as exc:  # noqa: BLE001
        outcome = f"raised {type(exc).__name__}"
        typed_ok = bool(_boundary_types()) and isinstance(exc, _boundary_types())
    assert typed_ok, f"FO-7n neighbour: fetch_remote_branch on network failure {outcome} (expected typed ExternalBoundaryError, never 'no branch')"


@pytest.mark.asyncio
async def test_FO6x_exact_path_bare_runtime_error_at_push_ends_terminal(db_session, superuser_db, owner_id, monkeypatch, github):
    async def boom(worktree):
        raise RuntimeError("transport refused")

    monkeypatch.setattr(execution_job, "push_worktree_branch", boom)
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    await drive(superuser_db, goal.id, rounds=5)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    takeovers = superuser_db.execute(text(
        "SELECT count(*) FROM mainai_recovery_records WHERE task_id=:t AND takeover_job_id IS NOT NULL"), {"t": str(edit.id)}).scalar_one()
    records = superuser_db.execute(text(
        "SELECT status::text, coalesce(classification::text,''), coalesce(blocker,'') FROM mainai_recovery_records WHERE task_id=:t"),
        {"t": str(edit.id)}).all()
    detail = (f"task={edit.status.value} jobs={jobs_for_task(superuser_db, edit.id)} takeovers={takeovers} recovery={[tuple(r) for r in records]} "
              f"paid={len(paid.calls)}")
    assert len(paid.kinds("code_agent")) == 1 and jobs_for_task(superuser_db, edit.id) == 1, "FO-6x containment broken: " + detail
    assert edit.status in (MainAITaskStatus.failed, MainAITaskStatus.blocked, MainAITaskStatus.cancelled), (
        "FO-6x NOT CLOSED (takeover cap never engages: no takeover is attempted): " + detail
    )
