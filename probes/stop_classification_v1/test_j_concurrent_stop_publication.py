"""J — Concurrent STOP vs draft-PR publication, and account erasure after a complete cycle
(criteria CS-1..CS-3, ER-4).

CS: the goal is STOPped from a concurrent session while the open_pr job is mid-flight (after its
work began, before the PR call). Real worker, fake GitHub REST, real locks.
  CS-1  no pull request is created after the STOP commits;
  CS-2  the open_pr task never becomes `completed` / `waiting_ci`;
  CS-3  no paid call after the STOP.
ER-4: after a complete first cycle (repo_edit pushed, draft PR opened, CI-green STOP-before-merge),
`erase_account_data` succeeds and leaves no execution rows for the owner.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.integrations.github_client import GitHubClient
from app.mainai_execution import execution_job
from app.models.mainai_execution import MainAITask, MainAITaskStatus
from app.models.mainai_wait import MainAITaskWait, MainAITaskWaitStatus

from tests.backend.test_account_erasure import _narrow_privileges_before_this_module  # noqa: F401  (EXECUTE on erasure functions)
from _harness import (  # noqa: F401
    PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, jobs_for_task, owner_id,
    plan_payload, planned_goal, tasks_by_type, trail,
)


def _stop(goal_id, task_id):
    from app.db import migration_engine
    from app.mainai_execution.first_cycle_stop import STATE_STOP_BEFORE_MERGE, stop_first_cycle

    with sessionmaker(bind=migration_engine)() as s:
        stop_first_cycle(s, s.get(MainAITask, task_id), reason="examiner concurrent STOP",
                         first_cycle_state=STATE_STOP_BEFORE_MERGE, evidence={"review_ok": True, "ci_passed": True})
        s.commit()


@pytest.mark.asyncio
async def test_CS_concurrent_stop_prevents_draft_pr(db_session, superuser_db, owner_id, monkeypatch, github):
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    tasks = tasks_by_type(db_session, goal.id)
    edit, pr_task = tasks["repo_edit"][0], tasks["open_pr"][0]
    state = {}
    original = execution_job._handle_open_pr

    async def stop_then_open_pr(db, task, goal_obj, **kwargs):
        # The open_pr job is running (claimed, dispatched) but has not yet taken the publication lock.
        if "stopped" not in state:
            t = threading.Thread(target=lambda: _stop(goal.id, edit.id), daemon=True)
            t.start()
            t.join(60)
            state["stopped"] = not t.is_alive()
            state["mark"] = len(paid.calls)
        return await original(db, task, goal_obj, **kwargs)

    monkeypatch.setattr(execution_job, "_handle_open_pr", stop_then_open_pr)
    await drive(superuser_db, goal.id, rounds=3)
    superuser_db.expire_all()
    pr_now = superuser_db.get(MainAITask, pr_task.id)
    detail = f"stopped={state.get('stopped')} prs={github.prs} open_pr={pr_now.status.value} paid_after={paid.since(state.get('mark', 0))} trail={trail(superuser_db, goal.id)[-4:]}"
    assert state.get("stopped") is True, "STOP did not commit promptly while open_pr ran: " + detail
    assert github.prs == [], "CS-1 draft PR created after STOP: " + detail
    assert pr_now.status not in (MainAITaskStatus.completed, MainAITaskStatus.waiting_ci), "CS-2 " + detail
    assert paid.since(state["mark"]) == [], "CS-3 " + detail


@pytest.mark.asyncio
async def test_ER4_account_erasure_after_complete_first_cycle(db_session, superuser_db, owner_id, monkeypatch, github, make_verified_user):
    from app.account.erasure import erase_account_data
    from app.account.reauth import create_account_erasure_reauth_receipt
    from app.models.user import User
    from tests.backend.test_account_erasure import _issue_erasure_access_session

    install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)

    async def green(db, wait):
        wait.status = MainAITaskWaitStatus.satisfied
        wait.evidence = {"scripted": "ci green"}
        wait.resolved_at = datetime.utcnow()
        db.flush()
        return wait

    monkeypatch.setattr(execution_job, "poll_ci_wait", green)
    await drive(superuser_db, goal.id, rounds=3)
    tasks = tasks_by_type(superuser_db, goal.id)
    pr_status = tasks["open_pr"][0].status.value
    assert github.prs, f"cycle did not open a draft PR: {trail(superuser_db, goal.id)[-4:]}"
    owner = superuser_db.get(User, owner_id)
    from tests.backend.test_account_erasure import _set_rls_user

    _set_rls_user(db_session, owner_id)
    jti = _issue_erasure_access_session(db_session, owner_id)
    user = db_session.get(User, owner_id)
    receipt = create_account_erasure_reauth_receipt(db_session, user=user, password="CorrectHorseBattery9!", access_jti=jti)
    _set_rls_user(db_session, owner_id, access_jti=receipt.access_jti)
    try:
        erase_account_data(db_session, user, reauth_receipt_id=receipt.receipt_id, reauth_access_jti=receipt.access_jti)
        ok, err = True, ""
    except Exception as exc:  # noqa: BLE001
        ok, err = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"
        db_session.rollback()
    left = superuser_db.execute(text("SELECT count(*) FROM mainai_tasks WHERE owner_id=:o"), {"o": str(owner_id)}).scalar_one()
    assert ok and left == 0, f"ER-4 account erasure after a complete cycle (open_pr={pr_status}, prs={len(github.prs)}): ok={ok} err={err} tasks_left={left}"


@pytest.mark.asyncio
async def test_CS_obs_stop_landing_inside_publication_waits_for_it(db_session, superuser_db, owner_id, monkeypatch, github):
    """Observation (P2 availability, not a bypass): a STOP that arrives after open_pr took the goal
    publication lock waits until the publication transaction commits; publication wins and the STOP
    lands afterwards. Records how long the STOP was blocked."""
    import time

    install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    edit = tasks_by_type(db_session, goal.id)["repo_edit"][0]
    state = {}
    original = execution_job._find_repo_edit_branch

    def stop_inside(db, *, goal_id):
        result = original(db, goal_id=goal_id)
        if "t" not in state:
            state["t0"] = time.monotonic()
            t = threading.Thread(target=lambda: (_stop(goal.id, edit.id), state.__setitem__("t1", time.monotonic())), daemon=True)
            t.start()
            t.join(3)
            state["t"] = t
            state["blocked_while_publishing"] = t.is_alive()
        return result

    monkeypatch.setattr(execution_job, "_find_repo_edit_branch", stop_inside)
    await drive(superuser_db, goal.id, rounds=2)
    state["t"].join(60)
    waited = round(state.get("t1", 0) - state["t0"], 2) if "t1" in state else None
    print(f"\nCS_OBS|stop_blocked_while_publishing={state['blocked_while_publishing']}|stop_committed_after_s={waited}|prs={len(github.prs)}")
    assert "t1" in state, "STOP never committed after publication finished"
