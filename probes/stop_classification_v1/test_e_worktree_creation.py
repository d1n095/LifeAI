"""E — External failure BEFORE the code agent: fetching the approved base during worktree creation
(criterion FO-7, added at the #288 examination).

The task worktree is built with `git fetch --depth 1 origin <base_sha>` against the real remote.
When GitHub/network is unavailable at that moment, the failure is external, exactly like a push
failure, and must not lead to paid work:
  FO-7  the repo_edit task ends terminal `failed`; no code-agent call; exactly one planner call
        (the original plan — no paid replan); exactly one job.
"""

from __future__ import annotations

import subprocess

import pytest

from app.mainai_execution import worktree as worktree_module
from app.models.mainai_execution import MainAITaskStatus

from _harness import (  # noqa: F401
    PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, jobs_for_task, owner_id,
    plan_payload, planned_goal, tasks_by_type, trail,
)

STDERR_NETWORK = "fatal: unable to access 'https://github.com/test-owner/test-repo.git/': Could not resolve host: github.com"


@pytest.mark.asyncio
async def test_FO7_base_fetch_network_failure_is_terminal_and_never_paid(db_session, superuser_db, owner_id, monkeypatch, github):
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 128, "", STDERR_NETWORK)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree_module.subprocess, "run", run)
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    await drive(superuser_db, goal.id, rounds=6)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    jobs = jobs_for_task(superuser_db, edit.id)
    detail = (f"status={edit.status.value} jobs={jobs} code_agent={len(paid.kinds('code_agent'))} planner={len(paid.kinds('planner'))} "
              f"trail={trail(superuser_db, goal.id)[-4:]}")
    assert len(paid.kinds("planner")) == 1 and len(paid.kinds("code_agent")) == 0, "FO-7 paid work after base-fetch network failure: " + detail
    assert edit.status == MainAITaskStatus.failed and jobs == 1, "FO-7 " + detail
