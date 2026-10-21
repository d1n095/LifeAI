"""F — What happens after an UNTYPED (bare RuntimeError) failure at the push boundary (FO-6 follow-up).

#288 deliberately re-raises a bare RuntimeError at the push boundary as a worker crash so recovery
can classify the local state (LOCAL_COMMITTED_NOT_PUSHED). This probe records what the real worker
does next, after leases expire and all ticks run:
  FO-6b  no paid call and no automatic re-execution (takeover) without Founder approval;
  FO-6c  the outcome is Founder-visible: a durable recovery record exists for the task.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.mainai_execution import execution_job

from _harness import (  # noqa: F401
    PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, jobs_for_task, owner_id,
    plan_payload, planned_goal, tasks_by_type, trail,
)


@pytest.mark.asyncio
async def test_FO6b_bare_runtime_error_at_push_is_contained_and_founder_visible(db_session, superuser_db, owner_id, monkeypatch, github):
    original = execution_job.push_worktree_branch

    async def boom(worktree):
        raise RuntimeError("authoritative worktree HEAD no longer matches the grant")

    monkeypatch.setattr(execution_job, "push_worktree_branch", boom)
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    await drive(superuser_db, goal.id, rounds=5)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    records = superuser_db.execute(text(
        "SELECT r.status::text, r.classification::text FROM mainai_recovery_records r WHERE r.task_id=:t ORDER BY r.created_at"),
        {"t": str(edit.id)}).all()
    print(f"\nFO6b|task={edit.status.value}|jobs={jobs_for_task(superuser_db, edit.id)}|paid={paid.calls}|recovery={[tuple(r) for r in records]}|trail={trail(superuser_db, goal.id)[-3:]}")
    assert len(paid.kinds("code_agent")) == 1 and len(paid.kinds("planner")) == 1 and jobs_for_task(superuser_db, edit.id) == 1, "FO-6b paid or re-executed"
    # FO-6c: an ambiguous publication-boundary failure must leave a Founder-visible observation of the ambiguity
    # (R5) -- either a mainai_recovery_records row, or a durable task event flagged reconcilable / needing
    # remote-ref observation. A bare `failed` with no such marker (the #294 behaviour) does not qualify.
    reconcilable_event = superuser_db.execute(text(
        "SELECT count(*) FROM mainai_task_events WHERE task_id=:t AND (detail->>'reconcilable')::text='true'"),
        {"t": str(edit.id)}).scalar_one()
    assert records or reconcilable_event, ("FO-6c no Founder-visible observation (recovery record or reconcilable event) "
                                           "for the stranded/ambiguous push")
