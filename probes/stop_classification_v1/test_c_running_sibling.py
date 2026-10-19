"""C — A sibling already RUNNING when the goal reaches terminal first-cycle STOP (criteria RS-1..RS-3).

Scenario (real worker, real git): the goal has two repo_edit chains, A (greeting) and
B (farewell). While B's job is mid-flight — after the paid code-agent call and the local commit,
before verification and push — chain A's goal-level STOP is committed from a separate session.
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

    original_commit = execution_job.commit_worktree_changes

    def commit_then_stop(db, worktree, *, message):
        result = original_commit(db, worktree, message=message)
        if worktree.task_id == b.id and state["stopped_at_call"] is None:
            state["a_status_at_stop"] = db.get(MainAITask, a.id).status.value
            state["b_job"] = worktree.job_id
            _stop_goal_from_other_session(goal.id, a.id)
            state["stopped_at_call"] = len(paid.calls)
        return result

    monkeypatch.setattr(execution_job, "commit_worktree_changes", commit_then_stop)
    await drive(superuser_db, goal.id, rounds=3)

    assert state["stopped_at_call"] is not None, f"scenario did not reach B's commit: trail={trail(superuser_db, goal.id)}"
    superuser_db.expire_all()
    b_now = superuser_db.get(MainAITask, b.id)
    b_branch = f"claude/mainai-task-{b.id}"
    job = superuser_db.get(MainAIJob, state["b_job"]) if state["b_job"] else None
    assert state.get("a_status_at_stop") == "completed", f"scenario precondition: A must be completed at STOP, was {state.get('a_status_at_stop')}"
    detail = (f"B status={b_now.status.value}; remote task branches={task_branches(github.bare)}; "
              f"B job cancel_requested={getattr(job, 'cancel_requested', None)}; paid after STOP={paid.since(state['stopped_at_call'])}; "
              f"trail={trail(superuser_db, goal.id)[-5:]}")
    assert b_branch not in task_branches(github.bare), "RS-1 running sibling pushed after goal STOP: " + detail
    assert job is not None and job.cancel_requested, "RS-2 running sibling job not cancel-requested by STOP: " + detail
    assert paid.since(state["stopped_at_call"]) == [] and b_now.status != MainAITaskStatus.completed, "RS-3 " + detail
