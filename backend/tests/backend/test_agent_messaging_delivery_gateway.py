"""Production delivery gateway: real app, worker, DB, and local authorized bridge."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.delivery import (
    attempt_transmit,
    ensure_delivery,
    issue_bridge_credential,
    prepare_message,
)
from app.agent_coordination.durable import load_owner_ledger
from app.agent_coordination.github_intake import passing_required_runs
from app.agent_coordination.lifecycle import ingest_lifecycle_event
from app.agent_coordination.local_bridge import LocalAuthorizedBridge
from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker

HEAD = "a786dac9f32bc3120fa2176e18041a75f1cf6c56"
PARENT = "2c9dd7e06dd3072f8f1f31531437f285ae978e5f"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    return user.id


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _goal_task(db, owner_id, title="Delivery"):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title=title,
        original_instruction="Deliver work without founder transcription.",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db,
        goal=goal,
        rationale="delivery",
        tasks=[PlannedTaskSpec(description="Build", task_type="repo_edit", target_files=["docs/DURABLE_AGENT_COORDINATION.md"])],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    return goal, task


def _agent(db, key, *, write=True):
    return register_agent(db, agent_key=key, display_name=key, adapter_kind="cli", supports_write=write)


def _assign(db, owner_id, goal, task, agent, *, role="builder", key="work", paths=None):
    return create_work_assignment(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role=role,
        read_write_mode="read_write" if role == "builder" else "read_only",
        repository_identity="lifeai",
        allowed_paths=paths or (["docs/**"] if role == "builder" else ["docs/**"]),
        requested_by="founder",
        provenance={"key": key},
    )


def _run_to(db, assignment, *statuses):
    for status in statuses:
        transition_status(db, assignment=assignment, new_status=status)
    db.commit()


def _worker_tick(db):
    Worker()._reconcile_durable_coordination(db)
    db.commit()


def _message_for(view, recipient):
    return next((row for row in view["delivery"]["messages"] if row["recipient"] == recipient), None)


def test_inbox_created_is_not_delivered(db_session, owner_id):
    goal, task = _goal_task(db_session, owner_id, title="Prepared only")
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    assignment = _assign(db_session, owner_id, goal, task, cursor, key="prep")
    _run_to(db_session, assignment, "ready", "running")
    service = load_owner_ledger(db_session, owner_id=owner_id)
    from app.agent_coordination.lifecycle import reconcile_owner

    reconcile_owner(db_session, owner_id=owner_id)
    service = load_owner_ledger(db_session, owner_id=owner_id)
    prepared = prepare_message(
        service,
        obligation_id=str(assignment.id),
        recipient="cursor",
        instructions="Do the work",
        idempotency_key=f"{assignment.id}:inbox",
    )
    assert prepared["outcome"] == "prepared"
    assert prepared["status"] == "prepared"
    assert prepared["message"]["status"] != "delivered"


def test_forged_identity_malicious_payload_and_unauthenticated_http(db_session, owner_id, client):
    goal, task = _goal_task(db_session, owner_id, title="Forged")
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    assignment = _assign(db_session, owner_id, goal, task, cursor, key="forged")
    _run_to(db_session, assignment, "ready", "running")
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    _worker_tick(db_session)
    assert client.get("/api/mainai/agent-gateway/inbox").status_code in {401, 403}
    spoof = client.get(
        "/api/mainai/agent-gateway/inbox",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert spoof.status_code in {401, 403}
    from app.agent_coordination.delivery import submit_receipt

    service = load_owner_ledger(db_session, owner_id=owner_id)
    row = next(iter(ensure_delivery(service)["messages"].values()))
    forged = submit_receipt(
        db_session,
        owner_id=owner_id,
        recipient="cursor",
        service=service,
        payload={
            "kind": "acknowledge",
            "event_id": "spoof-ack",
            "message_id": row["message_id"],
            "agent_identity": "claude",
        },
    )
    assert forged["outcome"] == "forged_rejected"
    malicious = submit_receipt(
        db_session,
        owner_id=owner_id,
        recipient="cursor",
        service=service,
        payload={
            "kind": "acknowledge",
            "event_id": "evil",
            "message_id": row["message_id"],
            "independently_verified": True,
        },
    )
    assert malicious["outcome"] == "forged_rejected"


def test_expired_duplicate_missed_ack_and_disconnected(db_session, owner_id, client):
    goal, task = _goal_task(db_session, owner_id, title="Edges")
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    assignment = _assign(db_session, owner_id, goal, task, cursor, key="edges")
    _run_to(db_session, assignment, "ready", "running")
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    _worker_tick(db_session)
    service = load_owner_ledger(db_session, owner_id=owner_id)
    cursor_msg = next(row for row in ensure_delivery(service)["messages"].values() if row["recipient"] == "cursor")
    assert cursor_msg["status"] == "available"
    bridge = LocalAuthorizedBridge(token=issued["token"], request=client.request)
    first = bridge.pull()
    assert first and first[0]["status"] == "delivered"
    second = bridge.claim(cursor_msg["message_id"])
    assert second["outcome"] == "replay_ignored"
    assert second["status"] == "delivered"
    # Missed acknowledgment stays delivered, never fabricated as acknowledged.
    db_session.expire_all()
    _set_rls(db_session, owner_id)
    restarted = load_owner_ledger(db_session, owner_id=owner_id)
    still = ensure_delivery(restarted)["messages"][cursor_msg["message_id"]]
    assert still["status"] == "delivered"
    assert still.get("acknowledged_at") is None
    expired = prepare_message(
        restarted,
        obligation_id=str(assignment.id),
        recipient="cursor",
        instructions="late",
        idempotency_key=f"{assignment.id}:expired",
        ttl_seconds=60,
    )
    expired_id = expired["message"]["message_id"]
    ensure_delivery(restarted)["messages"][expired_id]["expires_at"] = "2000-01-01T00:00:00.000000Z"
    expired_result = attempt_transmit(restarted, message_id=expired_id)
    assert expired_result["outcome"] == "failed"
    disconnected = issue_bridge_credential(db_session, owner_id=owner_id, recipient="claude", actor="worker")
    assert disconnected["outcome"] == "forged_rejected"


@pytest.mark.asyncio
async def test_real_bridge_round_trip_three_agent_restart(db_session, owner_id, client):
    goal, task = _goal_task(db_session, owner_id, title="Three-agent delivery")
    plan = create_plan(
        db_session,
        goal=goal,
        rationale="codex sibling",
        tasks=[PlannedTaskSpec(description="Codex build", task_type="repo_edit", target_files=["backend/app/agent_coordination/delivery.py"])],
        created_by="founder",
    )
    db_session.commit()
    codex_task = db_session.query(MainAITask).filter_by(plan_id=plan.id).one()
    cursor = _agent(db_session, f"cursor-{uuid.uuid4().hex[:8]}")
    claude = _agent(db_session, f"claude-{uuid.uuid4().hex[:8]}", write=False)
    codex = _agent(db_session, f"codex-{uuid.uuid4().hex[:8]}")
    cursor_a = _assign(db_session, owner_id, goal, task, cursor, key="cursor-build", paths=["docs/**"])
    claude_a = _assign(db_session, owner_id, goal, task, claude, role="reviewer", key="claude-exam", paths=["docs/**"])
    codex_a = _assign(db_session, owner_id, goal, codex_task, codex, key="codex-build", paths=["backend/**"])
    _run_to(db_session, cursor_a, "ready", "running")
    _run_to(db_session, claude_a, "ready", "running")
    _run_to(db_session, codex_a, "ready", "running")
    issued = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    assert issued["outcome"] == "issued"
    db_session.commit()

    _worker_tick(db_session)
    await Worker().run_once()
    view = load_owner_ledger(db_session, owner_id=owner_id).founder_view()
    cursor_msg = _message_for(view, "cursor")
    claude_msg = _message_for(view, "claude")
    codex_msg = _message_for(view, "codex")
    assert cursor_msg["status"] == "available"
    assert cursor_msg["status"] != "delivered"
    assert claude_msg["status"] == "blocked"
    assert claude_msg["missing_capability"] == "local_authorized_bridge"
    assert codex_msg["status"] == "blocked"
    assert all(row["vendor_delivery_claimed"] is False for row in (cursor_msg, claude_msg, codex_msg))

    bridge = LocalAuthorizedBridge(token=issued["token"], request=client.request)
    delivered = bridge.pull()
    assert len(delivered) == 1
    assert delivered[0]["status"] == "delivered"
    message_id = delivered[0]["message"]["message_id"]
    ack = bridge.acknowledge(message_id, event_id="ack-1")
    assert ack["status"] == "acknowledged"
    executing = bridge.report_execution(
        message_id,
        event_id="exec-1",
        evidence={"kind": "adapter_heartbeat", "source": "local_authorized_bridge"},
    )
    assert executing["status"] == "executing"
    completed = bridge.complete(message_id, event_id="done-1")
    assert completed["status"] == "completed"
    assert completed["independently_verified"] is False
    assert completed["vendor_delivery_claimed"] is False

    db_session.expire_all()
    _set_rls(db_session, owner_id)
    restarted = Worker()
    restarted._reconcile_durable_coordination(db_session)
    db_session.commit()
    await restarted.run_once()
    ledger = load_owner_ledger(db_session, owner_id=owner_id)
    cursor_row = ledger.get(str(cursor_a.id))
    assert cursor_row["status"] in {"completed", "executing", "acknowledged"}
    persisted = ensure_delivery(ledger)["messages"][message_id]
    assert persisted["status"] == "completed"

    _run_to(db_session, cursor_a, "completed")
    checks, workflows = passing_required_runs(HEAD)
    ingest_lifecycle_event(
        db_session,
        owner_id=owner_id,
        actor="worker",
        payload={
            "kind": "github_observation",
            "event_id": "pr-published-delivery",
            "observation": {
                "number": 301,
                "title": "lifecycle",
                "draft": False,
                "merged": False,
                "state": "open",
                "html_url": "https://github.com/d1n095/LifeAI/pull/301",
                "head_sha": HEAD,
                "base_sha": PARENT,
                "head_branch": "cursor/lifecycle-autonomous-coordination-91c1",
                "base_branch": "cursor/github-readonly-intake-91c1",
                "ancestry": [HEAD, PARENT],
                "reviews": [],
                "check_runs": checks,
                "workflow_runs": workflows,
            },
        },
    )
    db_session.commit()
    _worker_tick(db_session)
    final = load_owner_ledger(db_session, owner_id=owner_id).founder_view()
    examiners = [row for row in final["obligations"] if row["agent_identity"] == "examiner"]
    assert len(examiners) == 1
    examiner_msg = _message_for(final, "examiner")
    assert examiner_msg is not None
    assert examiner_msg["status"] == "blocked"
    assert examiner_msg["missing_capability"] == "local_authorized_bridge"
    assert final["first_cycle_go"] is False
    assert final["delivery"]["vendor_delivery_claimed"] is False
    snap = build_founder_ops_snapshot(db_session, owner_id=owner_id, health={"status": "ok"})
    assert snap["first_cycle_go"] is False
    assert snap["durable_coordination"]["delivery"]["vendor_delivery_claimed"] is False
    adapters = {row["channel"]: row for row in snap["durable_coordination"]["delivery"]["adapters"]}
    assert adapters["cursor_vendor_messaging"]["usable"] is False
    assert adapters["claude_vendor_messaging"]["usable"] is False
    assert adapters["codex_vendor_messaging"]["usable"] is False
    assert adapters["local_authorized_bridge"]["kind"] == "messaging"


def test_http_bridge_issue_requires_founder(client):
    res = client.post(
        "/api/mainai/founder-ops/agent-gateway/bridges",
        json={"recipient": "cursor"},
    )
    assert res.status_code in {401, 403}
