"""Examiner-only: can a billable Path A goal (live provider-spend grant, required by default since
#266) actually be dispatched by the real worker tick? No AI call, no GitHub, no agent."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.provider_spend import authorize_provider_spend, get_current_provider_spend_authorization
from app.request_context import current_user_id as current_user_id_var


def M(pid, holds, ev):
    print(f"\nMATRIX|{pid}|{'HOLDS' if holds else 'GAP'}|{ev}")


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _ready_task(db, owner_id, *, billable: bool):
    goal = planner.create_goal(db, owner_id=owner_id, title="first cycle", original_instruction="docstring + focused test", created_by="founder")
    planner.create_plan(db, goal=goal, rationale="exam", created_by="founder", tasks=[PlannedTaskSpec(
        description="Add a docstring to backend/app/x.py", task_type="read_only_audit", verification_plan=[])])
    db.commit()
    if billable:  # the ONLY way to satisfy MAINAI_EXECUTION_REQUIRE_PROVIDER_SPEND=true
        proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
        _, envelope = authorize_execution_scope(db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                                authorized_paths=["backend/app/x.py"], authorized_capabilities=["read_file"],
                                                authorized_risk="low", envelope_idempotency_key=f"e-{uuid.uuid4()}")
        authorize_provider_spend(db, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
                                 max_cost_usd=Decimal("1.00"), max_requests=3, max_cost_per_request_usd=Decimal("0.50"),
                                 idempotency_key=f"s-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=["gpt-4o-mini"])
        db.commit()
    return goal, db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()


def test_billable_path_a_goal_is_dispatched_by_the_worker(superuser_db, make_verified_user):
    from app.worker import Worker

    user, _ = make_verified_user()
    db = superuser_db
    current_user_id_var.set(str(user.id))
    db.execute(sa_text("SELECT set_config('app.current_user_id', :u, false)"), {"u": str(user.id)})
    control_goal, control_task = _ready_task(db, user.id, billable=False)
    goal, task = _ready_task(db, user.id, billable=True)
    live_grant = get_current_provider_spend_authorization(db, owner_id=user.id, goal_id=goal.id) is not None

    Worker()._advance_mainai_execution_tasks(db)
    db.commit()
    db.expire_all()
    control = db.get(MainAITask, control_task.id).status.value
    billable = db.get(MainAITask, task.id).status.value
    M("ROUTE-1 control: unbilled Path A goal is dispatched by the worker tick", control != "ready", f"status after tick={control}")
    M("ROUTE-2 billable Path A goal (live spend grant) is dispatched by the worker tick", billable != "ready",
      f"require_provider_spend default={get_settings().mainai_execution_require_provider_spend}; live grant={live_grant}; "
      f"status after tick={billable} (worker excludes every goal that has ever had an execution envelope; a grant requires one)")
