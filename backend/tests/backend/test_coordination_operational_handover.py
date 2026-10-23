"""DEF-1 / GH-4 persist + connect the existing gateway to available transports."""

from __future__ import annotations

import os
import stat
import uuid

import pytest
from sqlalchemy import select, text as sa_text

from app.agent_coordination.connect import founder_setup_requirements, run_authorized_receiver
from app.agent_coordination.delivery import issue_bridge_credential
from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.github_intake import passing_required_runs
from app.agent_coordination.lifecycle import ingest_lifecycle_event
from app.agent_coordination.persist import find_owner_assignments, persist_status
from app.agent_coordination.service import create_work_assignment, register_agent
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.models.agent_coordination import AgentWorkAssignment
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker

HEAD = "ff28b51429a5bc61f78308b657b8d57b2e5bf152"
PARENT = "a786dac9f32bc3120fa2176e18041a75f1cf6c56"


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    return user.id


def _observation(number=302):
    checks, workflows = passing_required_runs(HEAD)
    return {
        "number": number,
        "title": "delivery gateway",
        "draft": False,
        "merged": False,
        "state": "open",
        "html_url": f"https://github.com/d1n095/LifeAI/pull/{number}",
        "head_sha": HEAD,
        "base_sha": PARENT,
        "head_branch": "cursor/agent-messaging-delivery-gateway-91c1",
        "base_branch": "cursor/lifecycle-autonomous-coordination-91c1",
        "ancestry": [HEAD, PARENT],
        "reviews": [],
        "check_runs": checks,
        "workflow_runs": workflows,
    }


def _ingest_github(db, owner_id, event_id="gh-def1"):
    return ingest_lifecycle_event(
        db,
        owner_id=owner_id,
        actor="worker",
        payload={"kind": "github_observation", "event_id": event_id, "observation": _observation()},
    )


def test_def1_gh4_persists_without_prior_manual_assignment(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    assert find_owner_assignments(db_session, owner_id=owner_id) == []
    first = _ingest_github(db_session, owner_id, event_id="gh-cold-1")
    db_session.commit()
    assert first["outcome"] == "applied"
    assert first["builder_assignment_id"]
    assert first["examiner_assignment_id"]
    assert persist_status(db_session, owner_id=owner_id)["backing_present"] is True
    builder = first["builder_assignment_id"]
    examiner = first["examiner_assignment_id"]

    db_session.expire_all()
    _set_rls(db_session, owner_id)
    restarted = load_owner_ledger(db_session, owner_id=owner_id)
    ids = {row["assignment_id"] for row in restarted.founder_view()["obligations"]}
    assert builder in ids
    assert examiner in ids

    second = _ingest_github(db_session, owner_id, event_id="gh-cold-2")
    db_session.commit()
    assert second["builder_assignment_id"] == builder
    assert second["examiner_assignment_id"] == examiner
    assert second["duplicate_examiner"] is True

    db_session.expire_all()
    _set_rls(db_session, owner_id)
    Worker()._reconcile_durable_coordination(db_session)
    db_session.commit()
    again = load_owner_ledger(db_session, owner_id=owner_id)
    ids_after = {row["assignment_id"] for row in again.founder_view()["obligations"]}
    assert builder in ids_after
    assert examiner in ids_after
    builders = [row for row in again.founder_view()["obligations"] if row["workstream"] == "github-builder"]
    examiners = [row for row in again.founder_view()["obligations"] if row["workstream"] == "github-examiner"]
    assert len(builders) == 1
    assert len(examiners) == 1


def test_gh4b_existing_backing_row_stays_idempotent(db_session, make_verified_user):
    owner_id = _owner(db_session, make_verified_user)
    goal = create_goal(
        db_session,
        owner_id=owner_id,
        title="Existing assignment",
        original_instruction="GH-4b",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db_session,
        goal=goal,
        rationale="existing",
        tasks=[PlannedTaskSpec(description="Build", task_type="read_only_audit", verification_plan=[])],
        created_by="founder",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(db_session, agent_key=f"cursor-{uuid.uuid4().hex[:8]}", display_name="cursor", adapter_kind="cli")
    create_work_assignment(
        db_session,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="lifeai",
        allowed_paths=["docs/**"],
        requested_by="founder",
        provenance={"key": "gh4b"},
    )
    db_session.commit()
    first = _ingest_github(db_session, owner_id, event_id="gh4b-1")
    db_session.commit()
    second = _ingest_github(db_session, owner_id, event_id="gh4b-2")
    db_session.commit()
    assert first["builder_assignment_id"] == second["builder_assignment_id"]
    assert first["examiner_assignment_id"] == second["examiner_assignment_id"]
    assert db_session.execute(select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)).scalars().first() is not None


def test_authorized_local_cli_uses_existing_gateway(db_session, make_verified_user, client, tmp_path, monkeypatch):
    owner_id = _owner(db_session, make_verified_user)
    _ingest_github(db_session, owner_id, event_id="connect-1")
    db_session.commit()
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    Worker()._reconcile_durable_coordination(db_session)
    db_session.commit()

    script = tmp_path / "cursor-agent-probe"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CURSOR_AGENT", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CURSOR_AGENT", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_COMMAND__CURSOR_AGENT", str(script))
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ARGS__CURSOR_AGENT", "--probe")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    result = run_authorized_receiver(token=issued["token"], request=client.request, execute_local_cli=True, recipient="cursor")
    assert result["delivered_count"] == 1
    assert result["vendor_delivery_claimed"] is False
    receipt = result["receipts"][0]
    assert receipt["delivered"]["status"] == "delivered"
    assert receipt["ack"]["status"] == "acknowledged"
    assert receipt["cli"]["ran"] is True
    assert receipt["complete"]["status"] == "completed"
    assert receipt["complete"]["independently_verified"] is False

    db_session.expire_all()
    _set_rls(db_session, owner_id)
    Worker()._reconcile_durable_coordination(db_session)
    db_session.commit()
    view = load_owner_ledger(db_session, owner_id=owner_id).founder_view()
    handover = view["operational_handover"]
    assert handover["internal_coordination"]["examiner_prepared"] is True
    assert handover["local_authorized_bridge"]["completed"] >= 1
    assert handover["external_vendor_operations"]["usable"] is False
    assert handover["first_cycle_go"] is False
    snap = build_founder_ops_snapshot(db_session, owner_id=owner_id, health={"status": "ok"})
    assert snap["durable_coordination"]["operational_handover"]["external_vendor_operations"]["usable"] is False


def test_vendor_transports_remain_blocked_with_founder_setup():
    rows = {row["recipient"]: row for row in founder_setup_requirements()}
    for name in ("cursor", "claude", "codex"):
        assert rows[name]["vendor_messaging_usable"] is False
        assert rows[name]["missing_capability"] == "agent_messaging_adapter"
        assert rows[name]["founder_setup"]
    assert rows["github_publication"]["missing_capability"] == "codex_github_publication_authority"


def test_http_setup_and_refresh_require_founder(client):
    assert client.get("/api/mainai/founder-ops/agent-gateway/setup").status_code in {401, 403}
    assert client.post("/api/mainai/founder-ops/github-intake/refresh", json={"pr_numbers": [302]}).status_code in {401, 403}
