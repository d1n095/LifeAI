"""C — A sibling already RUNNING when the goal reaches terminal first-cycle STOP (criteria RS-1..RS-3).

Scenario (real worker, real git): the goal has two repo_edit chains, A (greeting) and
B (farewell). While B's job is mid-flight — after the paid code-agent call, the local commit and
the work_result checkpoint commit, at the start of verification, before push — chain A's
goal-level STOP is committed from a separate session (another thread, like a second worker).
  RS-0  the STOP commits promptly (<= 60 s) while B is running: a running job must not hold
        locks that block the STOP transaction (a blocked STOP is reported, not hung);
  RS-1  B's branch never reaches the remote (final pre-push authority check refuses);
  RS-2  B's running job is cooperatively cancel-requested by the STOP itself;
  RS-3  no paid call of any kind happens after the STOP; B never becomes `completed`.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import sessionmaker

from app.mainai_execution import execution_job
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob

from _harness import (  # noqa: F401
    OTHER_PATHS, PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, owner_id,
    plan_payload, planned_goal, task_branches, tasks_by_type, trail,
)


def _stop_goal_from_other_session(goal_id, a_task_id):
    from app.db import migration_engine
    from app.mainai_execution.first_cycle_stop import STATE_STOP_BEFORE_MERGE, stop_first_cycle

    session = sessionmaker(bind=migration_engine)()
    try:
        a = session.get(MainAITask, a_task_id)
        stop_first_cycle(session, a, reason="examiner: independent review recorded; STOP before merge",
                         first_cycle_state=STATE_STOP_BEFORE_MERGE, evidence={"review_ok": True, "ci_passed": True})
        session.commit()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_RS_running_sibling_cannot_push_after_goal_stop(db_session, superuser_db, owner_id, monkeypatch, github):
    paid = install_provider(
        monkeypatch,
        # B depends on A only to make the interleaving deterministic: A has COMPLETED (as in the real
        # case, where A's own CI/review outcome triggers the STOP) and B is mid-flight when STOP lands.
        plan=json.dumps({"tasks": [
            {**json.loads(plan_payload((PATHS, False)))["tasks"][0]},
            {**json.loads(plan_payload((OTHER_PATHS, False)))["tasks"][0], "depends_on": [0]},
        ]}),
        edit_for=lambda user: edit_payload(OTHER_PATHS if "farewell" in user else PATHS),
    )
    goal = await planned_goal(db_session, owner_id, paths=PATHS + OTHER_PATHS)
    edits = tasks_by_type(db_session, goal.id)["repo_edit"]
    a = next(t for t in edits if "greeting" in t.description)
    b = next(t for t in edits if "farewell" in t.description)
    state = {"stopped_at_call": None, "b_job": None, "a_status_at_stop": None}

    original_verify = execution_job.verify_task

    def stop_then_verify(task, *args, **kwargs):
        if task.id == b.id and state["stopped_at_call"] is None:
            import threading

            from app.db import migration_engine

            with sessionmaker(bind=migration_engine)() as probe:
                state["a_status_at_stop"] = probe.get(MainAITask, a.id).status.value
                state["b_job"] = probe.get(MainAITask, b.id).mainai_job_id
            outcome = {}

            def do_stop():
                try:
                    _stop_goal_from_other_session(goal.id, a.id)
                    outcome["ok"] = True
                except Exception as exc:  # deadlock / serialization failures are findings
                    outcome["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"

            t = threading.Thread(target=do_stop, daemon=True)
            t.start()
            t.join(60)
            state["stop_outcome"] = outcome if not t.is_alive() else {"blocked": "STOP still waiting after 60 s"}
            state["stop_thread"] = t
            state["stopped_at_call"] = len(paid.calls)
        return original_verify(task, *args, **kwargs)

    monkeypatch.setattr(execution_job, "verify_task", stop_then_verify)
    await drive(superuser_db, goal.id, rounds=3)
    if state.get("stop_thread") is not None:
        state["stop_thread"].join(120)

    assert state["stopped_at_call"] is not None, f"scenario did not reach B's commit: trail={trail(superuser_db, goal.id)}"
    superuser_db.expire_all()
    b_now = superuser_db.get(MainAITask, b.id)
    b_branch = f"claude/mainai-task-{b.id}"
    job = superuser_db.get(MainAIJob, state["b_job"]) if state["b_job"] else None
    assert state.get("a_status_at_stop") == "completed", f"scenario precondition: A must be completed at STOP, was {state.get('a_status_at_stop')}"
    detail = (f"B status={b_now.status.value}; remote task branches={task_branches(github.bare)}; "
              f"B job cancel_requested={getattr(job, 'cancel_requested', None)}; paid after STOP={paid.since(state['stopped_at_call'])}; "
              f"trail={trail(superuser_db, goal.id)[-5:]}")
    assert state.get("stop_outcome") == {"ok": True}, "RS-0 STOP did not commit promptly while sibling ran: " + str(state.get("stop_outcome")) + " | " + detail
    assert b_branch not in task_branches(github.bare), "RS-1 running sibling pushed after goal STOP: " + detail
    assert job is not None and job.cancel_requested, "RS-2 running sibling job not cancel-requested by STOP: " + detail
    assert paid.since(state["stopped_at_call"]) == [] and b_now.status != MainAITaskStatus.completed, "RS-3 " + detail
