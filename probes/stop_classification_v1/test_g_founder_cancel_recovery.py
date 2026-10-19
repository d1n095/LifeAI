"""G — Founder cancel followed by a worker crash, then recovery (criteria FC-1..FC-3, added at #289).

Real worker / real git. While a repo_edit job is mid-flight (after the paid code-agent call and the
local commit, at the start of verification) the Founder cancels the task through the real
executor.cancel_task() from a separate session; the worker then dies before acknowledging the
cancel (bare RuntimeError = crash). Leases expire and every worker tick runs.
  FC-1  the task ends `cancelled` (Founder cancel wins over recovery), never re-dispatched;
  FC-2  no takeover / fresh job and no paid call after the cancel;
  FC-3  an automatic or manual retry of the cancelled attempt is refused.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.mainai_execution import execution_job
from app.models.mainai_execution import MainAITask, MainAITaskStatus

from _harness import (  # noqa: F401
    PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, jobs_for_task, owner_id,
    plan_payload, planned_goal, tasks_by_type, trail,
)


@pytest.mark.asyncio
async def test_FC_founder_cancel_then_crash_is_not_taken_over(db_session, superuser_db, owner_id, monkeypatch, github):
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    edit = tasks_by_type(db_session, goal.id)["repo_edit"][0]
    state = {"mark": None}
    original_verify = execution_job.verify_task

    def cancel_then_crash(task, *args, **kwargs):
        if task.id == edit.id and state["mark"] is None:
            from app.db import migration_engine
            from app.mainai_execution.executor import cancel_task

            try:
                with sessionmaker(bind=migration_engine)() as founder:
                    t = founder.get(MainAITask, edit.id)
                    cancel_task(founder, task=t, cancelled_by="founder", cancelled_by_id=owner_id)
                    founder.commit()
            except Exception as exc:  # surfaced in the assertion, never swallowed by the crash path
                state["cancel_error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            state["mark"] = len(paid.calls)
            raise RuntimeError("worker process died after the Founder's cancel request")
        return original_verify(task, *args, **kwargs)

    monkeypatch.setattr(execution_job, "verify_task", cancel_then_crash)
    await drive(superuser_db, goal.id, rounds=5)
    superuser_db.expire_all()
    t = superuser_db.get(MainAITask, edit.id)
    jobs = jobs_for_task(superuser_db, edit.id)
    rec = superuser_db.execute(text("SELECT status::text, coalesce(classification::text,'') FROM mainai_recovery_records WHERE task_id=:t"),
                               {"t": str(edit.id)}).all()
    detail = (f"task={t.status.value} jobs={jobs} paid_after_cancel={paid.since(state['mark']) if state['mark'] is not None else 'n/a'} "
              f"recovery={[tuple(r) for r in rec]} trail={trail(superuser_db, goal.id)[-4:]}")
    assert state["mark"] is not None, "scenario did not reach verification: " + detail
    assert "cancel_error" not in state, "Founder cancel itself failed: " + state.get("cancel_error", "") + " | " + detail
    assert jobs == 1 and paid.since(state["mark"]) == [], "FC-2 takeover / paid call after Founder cancel: " + detail
    assert t.status == MainAITaskStatus.cancelled, "FC-1 Founder cancel did not terminalize the crashed attempt: " + detail
    from app.mainai_execution.executor import retry_task

    with pytest.raises(Exception):
        retry_task(superuser_db, task=t)
    superuser_db.rollback()
