"""Production Agent Messaging & Delivery Gateway certification (DLV-*) for #302.

Drives the REAL FastAPI app (TestClient), the REAL worker hook, the REAL LocalAuthorizedBridge over the production
agent-gateway HTTP routes, and the REAL persistence/reload. Independent emphasis vs the candidate's own suite:
  * cross-agent CLAIM and forgery through the authenticated HTTP endpoint (not only direct calls);
  * duplicate-execution prevention across a genuinely FRESH sessionmaker session (process restart);
  * transport honesty — the local bridge is not a Cursor/Claude/Codex vendor connection.
Credential-free, no paid calls, isolated DB. No production modification.
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


def _goal_task(db, owner, title="Delivery"):
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    goal = create_goal(db, owner_id=owner, title=title, original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(db, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/DURABLE_AGENT_COORDINATION.md"])])
    db.commit()
    return goal, db.query(MainAITask).filter_by(plan_id=plan.id).one()


def _assign(db, owner, goal, task, key, *, role="builder", paths=("docs/**",)):
    from app.agent_coordination.service import create_work_assignment, register_agent
    agent = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=(role == "builder"))
    return create_work_assignment(db, owner_id=owner, goal_id=goal.id, task_id=task.id, agent_id=agent.id,
                                  role=role, read_write_mode="read_write" if role == "builder" else "read_only",
                                  repository_identity="lifeai", allowed_paths=list(paths), requested_by="founder", provenance={"key": key})


def _run_to(db, a, *statuses):
    from app.agent_coordination.service import transition_status
    for s in statuses:
        transition_status(db, assignment=a, new_status=s)
    db.commit()


def _worker_tick(db):
    from app.worker import Worker
    Worker()._reconcile_durable_coordination(db)
    db.commit()


# ---- Item 1: an obligation yields EXACTLY ONE durable inbox message, correctly scoped, idempotent across reconciles
def test_DLV1_one_durable_idempotent_inbox_per_obligation(db_session, owner_id):
    from app.agent_coordination.delivery import ensure_delivery, issue_bridge_credential
    from app.agent_coordination.durable import load_owner_ledger
    goal, task = _goal_task(db_session, owner_id, title="Idem")
    a = _assign(db_session, owner_id, goal, task, "cursor")
    _run_to(db_session, a, "ready", "running")
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    _worker_tick(db_session); _worker_tick(db_session); _worker_tick(db_session)  # repeated reconciles must not duplicate
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    msgs = [r for r in ensure_delivery(svc)["messages"].values() if r["obligation_id"] == str(a.id)]
    print(f"DLV1|messages_for_obligation={len(msgs)} recipient={msgs[0]['recipient'] if msgs else None} status={msgs[0]['status'] if msgs else None} idem={msgs[0]['idempotency_key'] if msgs else None} expires={bool(msgs and msgs[0]['expires_at'])}")
    assert len(msgs) == 1, "DLV-1 an obligation produced duplicate inbox messages across reconciles"
    m = msgs[0]
    assert m["recipient"] == "cursor" and m["obligation_id"] == str(a.id)
    assert m["idempotency_key"] == f"{a.id}:inbox" and m["expires_at"]
    assert m["status"] == "available"  # live cursor bridge -> transmitted


# ---- Item 3: recipient isolation + cross-agent claim through the REAL authenticated HTTP endpoint
def test_DLV2_cross_agent_claim_rejected_over_http(db_session, owner_id, client):
    from app.agent_coordination.delivery import ensure_delivery, issue_bridge_credential
    from app.agent_coordination.durable import load_owner_ledger
    from app.limiter import limiter
    goal, task = _goal_task(db_session, owner_id, title="Cross")
    cur = _assign(db_session, owner_id, goal, task, "cursor")
    cld = _assign(db_session, owner_id, goal, task, "claude", role="reviewer")
    _run_to(db_session, cur, "ready", "running")
    _run_to(db_session, cld, "ready", "running")
    cursor_tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]
    db_session.commit()
    _worker_tick(db_session)
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    claude_msg = next(r for r in ensure_delivery(svc)["messages"].values() if r["recipient"] == "claude")
    # cursor's token lists ONLY cursor messages
    inbox = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {cursor_tok}"})
    recips = {m["recipient"] for m in inbox.json()["messages"]}
    # cursor's token tries to claim CLAUDE's message_id over HTTP -> must be refused
    cross = client.post(f"/api/mainai/agent-gateway/inbox/{claude_msg['message_id']}/claim", headers={"Authorization": f"Bearer {cursor_tok}"})
    print(f"DLV2|inbox_recipients={recips} cross_agent_claim_status={cross.status_code}")
    assert recips <= {"cursor"}, "DLV-2 inbox leaked another recipient's messages"
    assert cross.status_code == 403, "DLV-2 one agent claimed another agent's message over HTTP"


# ---- Item 3: forged token, replayed claim, forbidden-key fabrication through the REAL endpoints
def test_DLV3_forged_token_replay_and_fabrication_over_http(db_session, owner_id, client):
    from app.agent_coordination.delivery import issue_bridge_credential, ensure_delivery
    from app.agent_coordination.durable import load_owner_ledger
    goal, task = _goal_task(db_session, owner_id, title="Forge")
    a = _assign(db_session, owner_id, goal, task, "cursor")
    _run_to(db_session, a, "ready", "running")
    tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]
    db_session.commit()
    _worker_tick(db_session)
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    msg_id = next(r for r in ensure_delivery(svc)["messages"].values() if r["recipient"] == "cursor")["message_id"]
    anon = client.get("/api/mainai/agent-gateway/inbox")
    forged = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": "Bearer deadbeef.not.valid"})
    tampered = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok}x"})
    c1 = client.post(f"/api/mainai/agent-gateway/inbox/{msg_id}/claim", headers={"Authorization": f"Bearer {tok}"}).json()
    c2 = client.post(f"/api/mainai/agent-gateway/inbox/{msg_id}/claim", headers={"Authorization": f"Bearer {tok}"}).json()  # replay
    fab = client.post("/api/mainai/agent-gateway/receipts", headers={"Authorization": f"Bearer {tok}"},
                      json={"kind": "completion", "event_id": "evil-1", "message_id": msg_id, "override_stop": True, "github_write": True, "independently_verified": True})
    print(f"DLV3|anon={anon.status_code} forged={forged.status_code} tampered={tampered.status_code} claim1={c1.get('outcome')} claim2={c2.get('outcome')} fabrication={fab.status_code}")
    assert anon.status_code in (401, 403) and forged.status_code in (401, 403) and tampered.status_code in (401, 403)
    assert c1.get("outcome") == "delivered" and c2.get("outcome") == "replay_ignored", "DLV-3 replayed claim re-delivered"
    assert fab.status_code == 403, "DLV-3 a receipt carrying override_stop/github_write/independently_verified was accepted"


# ---- Item 4 + 5: a genuine bridge round trip, then duplicate-execution prevention across a TRUE fresh session
def test_DLV4_real_bridge_round_trip_survives_restart_no_double_exec(db_session, owner_id, client):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.delivery import ensure_delivery, issue_bridge_credential, submit_receipt
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.local_bridge import LocalAuthorizedBridge
    from app.db import migration_engine
    goal, task = _goal_task(db_session, owner_id, title="RoundTrip")
    a = _assign(db_session, owner_id, goal, task, "cursor")
    _run_to(db_session, a, "ready", "running")
    tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]
    db_session.commit()
    _worker_tick(db_session)
    bridge = LocalAuthorizedBridge(token=tok, request=client.request)
    delivered = bridge.pull()
    assert delivered and delivered[0]["status"] == "delivered", "DLV-4 bridge did not receive the message"
    mid = delivered[0]["message"]["message_id"]
    assert bridge.acknowledge(mid, event_id="ack-1")["status"] == "acknowledged"
    assert bridge.report_execution(mid, event_id="exec-1", evidence={"kind": "adapter_heartbeat"})["status"] == "executing"
    done = bridge.complete(mid, event_id="done-1")
    assert done["status"] == "completed" and done["independently_verified"] is False and done["vendor_delivery_claimed"] is False
    # TRUE RESTART: a brand-new session reloads persisted delivery state
    with sessionmaker(bind=migration_engine)() as fresh:
        _rls(fresh, owner_id)
        svc2 = load_owner_ledger(fresh, owner_id=owner_id)
        row = ensure_delivery(svc2)["messages"][mid]
        # duplicate completion with the SAME event_id after restart -> must be ignored, not re-executed
        dup = submit_receipt(fresh, owner_id=owner_id, recipient="cursor", service=svc2,
                             payload={"kind": "completion", "event_id": "done-1", "message_id": mid})
        obl = svc2.get(str(a.id))
    print(f"DLV4|restart_status={row['status']} obligation_status={obl.get('status') if obl else None} dup_completion={dup.get('outcome')}")
    assert row["status"] == "completed", "DLV-4 completed message did not survive a fresh session"
    assert dup.get("outcome") == "replay_ignored", "DLV-4 duplicate completion re-executed across restart"


# ---- Item 6: disconnected recipient, expired message, malformed/oversized payload
def test_DLV5_edges_disconnected_expired_malformed(db_session, owner_id, client):
    from app.agent_coordination.delivery import (attempt_transmit, ensure_delivery, issue_bridge_credential,
                                                 prepare_message, submit_receipt)
    from app.agent_coordination.durable import load_owner_ledger
    goal, task = _goal_task(db_session, owner_id, title="Edges")
    a = _assign(db_session, owner_id, goal, task, "cursor")
    _run_to(db_session, a, "ready", "running")
    db_session.commit()
    _worker_tick(db_session)  # NO bridge issued -> disconnected recipient
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    disc = next(r for r in ensure_delivery(svc)["messages"].values() if r["recipient"] == "cursor")
    # oversized instructions rejected
    big = prepare_message(svc, obligation_id=str(a.id), recipient="cursor", instructions="x" * 9000, idempotency_key=f"{a.id}:big")
    # expired message -> failed on transmit
    exp = prepare_message(svc, obligation_id=str(a.id), recipient="cursor", instructions="late", idempotency_key=f"{a.id}:exp", ttl_seconds=60)
    ensure_delivery(svc)["messages"][exp["message"]["message_id"]]["expires_at"] = "2000-01-01T00:00:00.000000Z"
    exp_res = attempt_transmit(svc, message_id=exp["message"]["message_id"])
    # malformed receipt kind rejected
    bad_kind = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc,
                              payload={"kind": "pwn", "event_id": "e", "message_id": disc["message_id"]})
    print(f"DLV5|disconnected_status={disc['status']} disconnected_missing={disc['missing_capability']} oversized={big.get('outcome')} expired={exp_res.get('outcome')} bad_kind={bad_kind.get('outcome')}")
    assert disc["status"] == "blocked" and disc["missing_capability"] == "local_authorized_bridge"
    assert big.get("outcome") == "rejected", "DLV-5 oversized instructions accepted"
    assert exp_res.get("outcome") == "failed", "DLV-5 expired message transmitted"
    assert bad_kind.get("outcome") == "rejected", "DLV-5 malformed receipt kind accepted"


# ---- Items 8, 11, 12: transport honesty — local bridge != vendor connection; no false certification
def test_DLV6_transport_honesty_and_no_false_certification(db_session, owner_id):
    from app.agent_coordination.delivery import issue_bridge_credential, transport_inventory
    from app.agent_coordination.durable import load_owner_ledger
    goal, task = _goal_task(db_session, owner_id, title="Transport")
    a = _assign(db_session, owner_id, goal, task, "cursor")
    _run_to(db_session, a, "ready", "running")
    db_session.commit()
    svc0 = load_owner_ledger(db_session, owner_id=owner_id)
    before = {r["channel"]: r for r in transport_inventory(svc0)}
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")
    db_session.commit()
    svc1 = load_owner_ledger(db_session, owner_id=owner_id)
    after = {r["channel"]: r for r in transport_inventory(svc1)}
    vendor = {k: v["usable"] for k, v in after.items() if v.get("kind") == "messaging" and "vendor" in k}
    print(f"DLV6|bridge_usable_before={before['local_authorized_bridge']['usable']} bridge_usable_after={after['local_authorized_bridge']['usable']} live={after['local_authorized_bridge']['live_recipients']} vendor={vendor}")
    assert before["local_authorized_bridge"]["usable"] is False, "DLV-6 bridge reported usable with no credential"
    assert after["local_authorized_bridge"]["usable"] is True and "cursor" in after["local_authorized_bridge"]["live_recipients"]
    assert after["local_authorized_bridge"]["delivery_claimed"] is False
    assert vendor and all(v is False for v in vendor.values()), "DLV-6 a vendor messaging channel claimed usable"
    # the local bridge is explicitly NOT a vendor API
    assert "vendor" not in (after["local_authorized_bridge"].get("reason") or "").lower() or "not a vendor" in after["local_authorized_bridge"]["reason"].lower()
