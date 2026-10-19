"""K — Is the crash-recovery loop after a persistent push-boundary failure bounded, and is it paid?
(criteria RL-1..RL-3, added after the harness 404-fidelity fix.)

A bare RuntimeError at the push boundary every time (e.g. a transport that keeps refusing with an
untyped error). Real worker; leases made due between rounds; 10 rounds.
  RL-1  the number of execution jobs for the task is bounded by the auto-recovery cap (<= 2);
  RL-2  no more than one extra paid code-agent call is made by recovery (salvage resumes the
        committed worktree instead of re-running the agent);
  RL-3  the loop ends in a Founder-visible terminal/blocked state.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.mainai_execution import execution_job
from app.models.mainai_execution import MainAITaskStatus

from _harness import (  # noqa: F401
    PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, jobs_for_task, owner_id,
    plan_payload, planned_goal, tasks_by_type, trail,
)


@pytest.mark.asyncio
async def test_RL_persistent_push_boundary_crash_recovery_is_bounded(db_session, superuser_db, owner_id, monkeypatch, github):
    async def boom(worktree):
        raise RuntimeError("transport refused")

    monkeypatch.setattr(execution_job, "push_worktree_branch", boom)
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    await drive(superuser_db, goal.id, rounds=10)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    rec = superuser_db.execute(text(
        "SELECT status::text, coalesce(classification::text,''), takeover_job_id IS NOT NULL, coalesce(blocker,'') "
        "FROM mainai_recovery_records WHERE task_id=:t ORDER BY detected_at"), {"t": str(edit.id)}).all()
    ev = superuser_db.execute(text(
        "SELECT event_type::text, count(*) FROM mainai_task_events WHERE task_id=:t GROUP BY 1 ORDER BY 1"), {"t": str(edit.id)}).all()
    jobs = jobs_for_task(superuser_db, edit.id)
    agent = len(paid.kinds("code_agent"))
    detail = f"task={edit.status.value} jobs={jobs} code_agent={agent} recovery={[tuple(r) for r in rec]} events={dict(ev)}"
    print("\nRL|" + detail)
    assert jobs <= 2, "RL-1 unbounded crash-recovery loop: " + detail
    assert agent <= 2, "RL-2 recovery re-runs the paid code agent: " + detail
    assert edit.status in (MainAITaskStatus.failed, MainAITaskStatus.blocked, MainAITaskStatus.cancelled), "RL-3 " + detail
