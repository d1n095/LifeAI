"""B — Local failures keep their legitimate retry / replan behaviour (criteria LR-1..LR-3).

LR-1  a genuine local verification failure (the edited test fails) is retried automatically with
      a fresh code-agent attempt, and once attempts are exhausted it remains replan-eligible;
LR-2  a local publication-scope refusal (agent wrote an out-of-scope file) is retried as local;
LR-3  (P2) model-controlled text (a filename containing classifier keywords) cannot turn a local
      failure into an external STOP.
"""

from __future__ import annotations

import pytest

from app.mainai_execution import replan as replan_mod
from app.models.mainai_execution import MainAIGoal, MainAITaskStatus

from _harness import (  # noqa: F401
    PATHS, _privileges, _production_spend_gate, drive, edit_payload, github, install_provider, owner_id, plan_payload,
    planned_goal, tasks_by_type, trail,
)


@pytest.mark.asyncio
async def test_LR1_local_verification_failure_retries_and_stays_replan_eligible(db_session, superuser_db, owner_id, monkeypatch, github):
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS, failing_test=True))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    # Freeze the worker's replan tick during the drive so THIS task's retry behaviour is observable;
    # replan eligibility is then checked with the real, unpatched function below.
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod, "find_replan_trigger", lambda db, goal: None)
    await drive(superuser_db, goal.id, rounds=5)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    attempts = len(paid.kinds("code_agent"))
    assert attempts >= 2, f"LR-1 local verification failure was not retried: code_agent={attempts} status={edit.status.value} trail={trail(superuser_db, goal.id)[-4:]}"
    assert edit.status in (MainAITaskStatus.failed, MainAITaskStatus.retryable_failed)
    if edit.status == MainAITaskStatus.failed:
        trigger = replan_mod.find_replan_trigger(superuser_db, goal=superuser_db.get(MainAIGoal, goal.id))
        assert trigger is not None and trigger.id == edit.id, "LR-1 exhausted local verification failure is no longer replan-eligible"


@pytest.mark.asyncio
async def test_LR2_local_scope_refusal_is_retried_as_local(db_session, superuser_db, owner_id, monkeypatch, github):
    stray = {"path": "backend/app/security_guard.py", "content": "X = 1\n"}
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS, extra_files=[stray]))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod, "find_replan_trigger", lambda db, goal: None)
    await drive(superuser_db, goal.id, rounds=3)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    assert len(paid.kinds("code_agent")) >= 2, f"LR-2 local refusal treated as terminal external: status={edit.status.value} trail={trail(superuser_db, goal.id)[-3:]}"


@pytest.mark.asyncio
async def test_LR3_model_controlled_filename_cannot_force_external_stop(db_session, superuser_db, owner_id, monkeypatch, github):
    """P2: the agent names an out-of-scope file whose path contains classifier keywords."""
    stray = {"path": "backend/app/approved_base_sha_push_grant_remote_rejected.py", "content": "X = 1\n"}
    paid = install_provider(monkeypatch, plan=plan_payload((PATHS, True)), edit_for=lambda u: edit_payload(PATHS, extra_files=[stray]))
    goal = await planned_goal(db_session, owner_id, paths=PATHS)
    import app.worker as worker_mod

    monkeypatch.setattr(worker_mod, "find_replan_trigger", lambda db, goal: None)
    await drive(superuser_db, goal.id, rounds=3)
    edit = tasks_by_type(superuser_db, goal.id)["repo_edit"][0]
    assert len(paid.kinds("code_agent")) >= 2, f"LR-3 model-chosen filename forced an external STOP: status={edit.status.value} trail={trail(superuser_db, goal.id)[-3:]}"
