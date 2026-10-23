"""Operational-handover certification (CONN-*) for #303 — DEF-1 backing rows + authorized receiver.

Independent of the candidate's own suite. Drives the REAL persist.py, the REAL agent-gateway over TestClient, the
REAL run_authorized_receiver + adapter_config fail-closed gating, and a genuinely FRESH session for restart/durability.
Credential-free, no paid calls, no network, isolated DB. No production modification.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401


def _rls(session, owner):
    from app.request_context import current_user_id as current_user_id_var
    current_user_id_var.set(str(owner))
    session.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner)})


def _seed(db, make_verified_user, key="cursor"):
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    user, _ = make_verified_user()
    owner = user.id
    _rls(db, owner)
    goal = create_goal(db, owner_id=owner, title="c", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(db, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=True)
    a = create_work_assignment(db, owner_id=owner, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="builder",
                               read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return owner, a


def _worker_tick(db):
    from app.worker import Worker
    Worker()._reconcile_durable_coordination(db)
    db.commit()


# ---- Item 4: cold-start backing row is owner-scoped, read-only, write-incapable, and isolated per owner
def test_CONN1_backing_row_owner_scoped_and_write_incapable(superuser_db, make_verified_user):
    from app.agent_coordination.persist import ensure_owner_ledger_backing, persist_status
    from app.models.user import User
    # two independent owners, neither with any prior assignment
    u1, _ = make_verified_user(); u2, _ = make_verified_user()
    superuser_db.commit()
    _rls(superuser_db, u1.id)
    b1 = ensure_owner_ledger_backing(superuser_db, owner_id=u1.id); superuser_db.commit()
    _rls(superuser_db, u2.id)
    b2 = ensure_owner_ledger_backing(superuser_db, owner_id=u2.id); superuser_db.commit()
    st1 = persist_status(superuser_db, owner_id=u1.id)
    print(f"CONN1|b1_owner={b1.owner_id} b2_owner={b2.owner_id} b1_rw={b1.read_write_mode} status={st1}")
    assert b1.owner_id == u1.id and b2.owner_id == u2.id and b1.id != b2.id, "CONN-1 backing rows not owner-isolated"
    assert str(b1.read_write_mode.value if hasattr(b1.read_write_mode, 'value') else b1.read_write_mode) == "read_only", "CONN-1 backing row is not read-only"
    assert st1["created_without_manual_assignment"] is True
    # idempotent: a second ensure for the same owner returns the SAME backing row, not a duplicate
    _rls(superuser_db, u1.id)
    b1b = ensure_owner_ledger_backing(superuser_db, owner_id=u1.id)
    assert b1b.id == b1.id, "CONN-1 a second cold-start ensure created a duplicate backing row"


# ---- Item 7/9: local CLI is fail-closed; an executable on PATH is NOT authorization
def test_CONN2_local_cli_fail_closed_without_founder_enablement(monkeypatch):
    from app.agent_coordination import adapter_config as ac
    from app.agent_coordination.connect import run_founder_local_cli
    # Not enabled at all -> fail closed
    r_off = run_founder_local_cli("cursor-agent")
    # Pretend the executable exists AND is enabled, but NO args template supplied -> still fail closed
    monkeypatch.setattr(ac.shutil, "which", lambda _x: "/usr/bin/cursor-agent")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CURSOR_AGENT", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CURSOR_AGENT", "true")
    avail = ac.adapter_availability("cursor-agent")
    r_noargs = run_founder_local_cli("cursor-agent")
    print(f"CONN2|off={r_off.get('ran')}/{r_off.get('missing_capability')} enabled_found={avail.enabled}/{avail.executable_found} noargs_ran={r_noargs.get('ran')}/{r_noargs.get('missing_capability')}")
    assert r_off.get("ran") is False and r_off.get("missing_capability") == "founder_adapter_enablement"
    assert avail.enabled is True and avail.executable_found is True  # executable present + enabled...
    assert r_noargs.get("ran") is False, "CONN-2 CLI ran with an executable on PATH but no founder-supplied argv template"
    assert r_off.get("vendor_delivery_claimed") is False and r_noargs.get("vendor_delivery_claimed") is False


# ---- Item 6/7: run_authorized_receiver drives the REAL gateway; CLI never runs without enablement; claim precedes any exec
def test_CONN3_authorized_receiver_real_gateway_no_cli(db_session, owner_id, client, monkeypatch):
    from app.agent_coordination.connect import run_authorized_receiver
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    _rls(db_session, owner_id)
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    goal = create_goal(db_session, owner_id=owner_id, title="c", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(db_session, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    task = db_session.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(db_session, agent_key="cursor-agent", display_name="cursor", adapter_kind="cli", supports_write=True)
    a = create_work_assignment(db_session, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="builder",
                               read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    from app.agent_coordination.service import transition_status
    for s in ("ready", "running"):
        transition_status(db_session, assignment=a, new_status=s)
    tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]
    db_session.commit()
    _worker_tick(db_session)
    # execute_local_cli=True but no adapter enabled -> message is delivered/ack'd/completed, CLI does NOT run
    result = run_authorized_receiver(token=tok, request=client.request, execute_local_cli=True, recipient="cursor")
    receipts = result["receipts"]
    cli_ran = [r["cli"].get("ran") for r in receipts]
    completes = [r["complete"].get("status") for r in receipts]
    print(f"CONN3|delivered={result['delivered_count']} cli_ran={cli_ran} completes={completes} vendor_claimed={result['vendor_delivery_claimed']}")
    assert result["delivered_count"] == 1 and completes == ["completed"], "CONN-3 receiver did not complete a real round trip"
    assert cli_ran == [False], "CONN-3 local CLI executed without founder enablement"
    assert result["vendor_delivery_claimed"] is False


# ---- Item 8: durable completion + duplicate-execution prevention across a TRUE fresh session
def test_CONN4_durable_completion_no_double_exec_across_restart(db_session, owner_id, client, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.connect import run_authorized_receiver
    from app.agent_coordination.delivery import ensure_delivery, issue_bridge_credential, submit_receipt
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
    from app.db import migration_engine
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    _rls(db_session, owner_id)
    goal = create_goal(db_session, owner_id=owner_id, title="c", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(db_session, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    task = db_session.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(db_session, agent_key="cursor-agent", display_name="cursor", adapter_kind="cli", supports_write=True)
    a = create_work_assignment(db_session, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="builder",
                               read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    for s in ("ready", "running"):
        transition_status(db_session, assignment=a, new_status=s)
    tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]
    db_session.commit()
    _worker_tick(db_session)
    result = run_authorized_receiver(token=tok, request=client.request, execute_local_cli=False, recipient="cursor")
    mid = result["receipts"][0]["delivered"]["message"]["message_id"]
    # TRUE restart: fresh session reloads persisted delivery state
    with sessionmaker(bind=migration_engine)() as fresh:
        _rls(fresh, owner_id)
        svc = load_owner_ledger(fresh, owner_id=owner_id)
        row = ensure_delivery(svc)["messages"][mid]
        dup = submit_receipt(fresh, owner_id=owner_id, recipient="cursor", service=svc,
                             payload={"kind": "completion", "event_id": f"connect-done-{mid[:8]}", "message_id": mid})
    print(f"CONN4|restart_status={row['status']} dup_completion={dup.get('outcome')}")
    assert row["status"] == "completed", "CONN-4 completion did not survive a fresh session"
    assert dup.get("outcome") == "replay_ignored", "CONN-4 duplicate completion re-executed across restart"


# ---- Item 10: operational-handover view honestly reports dependencies, never pretends an adapter is active
def test_CONN5_handover_view_is_honest(db_session, owner_id):
    from app.agent_coordination.connect import operational_handover_view
    from app.agent_coordination.durable import load_owner_ledger
    _rls(db_session, owner_id)
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    view = operational_handover_view(svc)
    ext = view["external_vendor_operations"]
    perms = {row["recipient"]: row for row in view["remaining_permissions"]}
    print(f"CONN5|external_usable={ext['usable']} founder_must_forward={view['founder_must_forward_messages']} vendor_claimed={view['vendor_delivery_claimed']} perms={list(perms)}")
    assert ext["usable"] is False and ext["cursor"] is False and ext["claude"] is False and ext["codex"] is False
    assert view["vendor_delivery_claimed"] is False and view["first_cycle_go"] is False
    assert view["founder_must_forward_messages"] is True, "CONN-5 claims handover with no delivery evidence"
    for r in ("cursor", "claude", "codex"):
        assert perms[r]["vendor_messaging_usable"] is False and perms[r]["founder_setup"]
    assert perms["github_publication"]["missing_capability"]  # Codex-owned, named
