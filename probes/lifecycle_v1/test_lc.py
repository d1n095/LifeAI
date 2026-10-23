"""Production-path lifecycle ingestion certification (LC-*) for #301.
Drives the REAL Founder endpoint POST /api/mainai/founder-ops/coordination/events via TestClient, and the REAL worker
hook Worker._reconcile_durable_coordination. Nothing is substituted in-process for the runtime. Credential-free, no paid.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
EVENTS = "/api/mainai/founder-ops/coordination/events"


def _csrf(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def _seed_assignment(db, make_verified_user, *, agent_key="cursor-agent", role="builder"):
    """Create a real owner + goal + plan + task + registered agent + AgentWorkAssignment (status planned)."""
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    from app.request_context import current_user_id as current_user_id_var

    user, _ = make_verified_user()
    owner = user.id
    current_user_id_var.set(str(owner))
    db.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner)})
    goal = create_goal(db, owner_id=owner, title="g", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(db, goal=goal, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    agent = register_agent(db, agent_key=agent_key, display_name=agent_key, adapter_kind="cli", supports_write=True)
    a = create_work_assignment(db, owner_id=owner, goal_id=goal.id, task_id=task.id, agent_id=agent.id,
                               role=role, read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return owner, a


# ---- Item 3: the events endpoint is Founder-only
def test_LC1_events_endpoint_is_founder_only(client, make_verified_user, db_session, monkeypatch):
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    member, mpw = make_verified_user()
    impostor, ipw = make_verified_user(role="founder")
    db_session.commit()
    ev = {"kind": "assignment_observed", "event_id": str(uuid.uuid4()), "agent_identity": "cursor"}
    assert client.post(EVENTS, json=ev).status_code in (401, 403)
    for email, pw in ((member.email, mpw), (impostor.email, ipw)):
        csrf = _csrf(client, email, pw)
        assert client.post(EVENTS, json=ev, headers={"X-CSRF-Token": csrf}).status_code == 403
        client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}); client.cookies.clear()
    print("LC1|anon/member/role-only-founder all refused")


# ---- Item 3: forged lifecycle kind / unknown agent identity rejected via the real endpoint
def test_LC2_forged_kind_and_identity_rejected(client, db_session, monkeypatch):
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    csrf = _csrf(client)
    bad_kind = client.post(EVENTS, json={"kind": "attacker_declares_done", "event_id": str(uuid.uuid4()), "agent_identity": "cursor"}, headers={"X-CSRF-Token": csrf}).json()
    bad_id = client.post(EVENTS, json={"kind": "assignment_observed", "event_id": str(uuid.uuid4()), "agent_identity": "attacker"}, headers={"X-CSRF-Token": csrf}).json()
    print(f"LC2|bad_kind={bad_kind.get('outcome')} bad_identity={bad_id.get('outcome')}")
    assert bad_kind.get("outcome") == "forged_rejected"
    assert bad_id.get("outcome") == "forged_rejected"


# ---- Item 1 + 3: replay protection on a genuinely-applied event survives a FRESH DB session / restart
def test_LC3_replay_protection_survives_restart(superuser_db, make_verified_user):
    """A random assignment_id is stale_rejected before dedup can record it (that only proves the stale guard). The real
    item-1 question is: once an event is APPLIED and persisted, is its event_id still rejected as a replay after the
    ledger is reloaded from the DB in a brand-new session (worker restart / next HTTP request)? Drives the real
    ingest_lifecycle_event + _persist_ledger + load_owner_ledger path, reloading between the two ingests."""
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    from app.db import migration_engine
    owner, a = _seed_assignment(superuser_db, make_verified_user)
    eid = f"lc3-{uuid.uuid4().hex[:8]}"
    ev = {"kind": "assignment_observed", "event_id": eid, "agent_identity": "cursor", "assignment_id": str(a.id)}
    r1 = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="founder", payload=dict(ev))
    superuser_db.commit()
    # RESTART: a brand-new session reloads the ledger from the DB and must still reject the duplicate event_id.
    with sessionmaker(bind=migration_engine)() as fresh:
        fresh.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner)})
        r2 = ingest_lifecycle_event(fresh, owner_id=owner, actor="founder", payload=dict(ev))
        fresh.commit()
    print(f"LC3|first={r1.get('outcome')} replay_after_restart={r2.get('outcome')}")
    assert r1.get("outcome") == "applied", "LC-3 precondition: the first event must actually apply"
    assert r2.get("outcome") == "replay_ignored", "LC-3 replay protection did NOT survive a fresh session (item 1)"


# ---- Item 6/9/C: a prepared instruction is never a claimed delivery; vendor messaging is BLOCKED
def test_LC4_delivery_never_claimed_and_inventory_blocked(client, db_session, monkeypatch):
    from app.agent_coordination.lifecycle import delivery_adapter_inventory
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    csrf = _csrf(client)
    r = client.post(EVENTS, json={"kind": "delivery_claim", "event_id": str(uuid.uuid4()), "agent_identity": "cursor", "to": "claude"}, headers={"X-CSRF-Token": csrf}).json()
    inv = {row["channel"]: row for row in delivery_adapter_inventory()}
    vendor = {k: v["usable"] for k, v in inv.items() if v.get("kind") == "messaging"}
    print(f"LC4|delivery_claimed={r.get('delivery_claimed')} vendor_messaging_usable={vendor}")
    assert r.get("delivery_claimed") is False, "LC-4 the endpoint claimed delivery to an agent"
    assert vendor and all(v is False for v in vendor.values()), "LC-4 a vendor messaging channel is marked usable (no authorized adapter exists)"
    assert inv["github_publication"]["usable"] is False


# ---- Item 2 + 7: a REAL assignment lifecycle is auto-projected into the ledger by the worker hook, restart-safe
def test_LC5_worker_reconcile_projects_real_events_into_ledger(superuser_db, make_verified_user):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.durable import founder_coordination_snapshot
    from app.agent_coordination.service import transition_status
    from app.db import migration_engine
    from app.worker import Worker

    owner, a = _seed_assignment(superuser_db, make_verified_user)
    # real workforce lifecycle: planned -> ready -> running -> completed (emits AgentWorkAssignmentEvent rows)
    for st in ("ready", "running", "completed"):
        transition_status(superuser_db, assignment=a, new_status=st)
    superuser_db.commit()
    # production worker hook (what Worker.run_once calls every tick) — NO manual coordination refresh
    Worker()._reconcile_durable_coordination(superuser_db)
    superuser_db.commit()
    # RESTART: fresh session + production Founder snapshot (dashboard reads persisted runtime state, item 7)
    with sessionmaker(bind=migration_engine)() as fresh:
        fresh.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner)})
        snap = founder_coordination_snapshot(fresh, owner_id=owner)
    obl = snap.get("obligations", [])
    print(f"LC5|obligations_after_worker_reconcile={len(obl)} statuses={[o.get('status') for o in obl]}")
    assert obl, "LC-5 the worker hook did not project the real assignment lifecycle into the durable ledger"
    assert any(o.get("status") in ("completed", "independently_verified", "acknowledged", "running") for o in obl)


# ---- Item 3: the payload's agent_identity is never trusted for attribution; the cross-agent guard holds for agents
def test_LC6_identity_cannot_be_forged_for_attribution(superuser_db, make_verified_user):
    """Two distinct properties:
    (a) endpoint path — the authenticated Founder (privileged actor) posts a completion for a CURSOR assignment but
        claims agent_identity=codex. The implementation NEVER attributes to the claimed identity: it maps to the
        assignment's real agent. So a forged payload identity cannot mis-attribute work. Outcome applies, attributed cursor.
    (b) direct non-privileged path — an agent 'codex' trying to complete a CURSOR assignment is forged_rejected.
    """
    from app.agent_coordination.lifecycle import ingest_lifecycle_event

    owner, a = _seed_assignment(superuser_db, make_verified_user)
    # (a) founder forges agent_identity=codex -> must be neutralized to the real assignment identity (cursor)
    forged_attr = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="founder",
                                          payload={"kind": "completion_claim", "event_id": f"lc6a-{uuid.uuid4().hex[:8]}",
                                                   "assignment_id": str(a.id), "agent_identity": "codex",
                                                   "evidence": {"execution_evidence": {"commit_sha": "a" * 40}}})
    ident = forged_attr.get("agent_identity") or forged_attr.get("obligation", {}).get("agent_identity")
    print(f"LC6a|outcome={forged_attr.get('outcome')} attributed={ident}")
    assert forged_attr.get("outcome") == "applied"
    assert ident != "codex", "LC-6a forged payload agent_identity was trusted for attribution"
    # (b) a non-privileged agent 'codex' completing cursor's assignment -> forged_rejected
    cross = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="codex",
                                   payload={"kind": "completion_claim", "event_id": f"lc6b-{uuid.uuid4().hex[:8]}",
                                            "assignment_id": str(a.id), "agent_identity": "codex",
                                            "evidence": {"execution_evidence": {"commit_sha": "b" * 40}}})
    print(f"LC6b|cross_agent_outcome={cross.get('outcome')} reason={cross.get('reason')}")
    assert cross.get("outcome") == "forged_rejected", "LC-6b one agent completed another agent's assignment"


# ---- Item 5 + 6: a historical queue-empty report cannot erase a newer active assignment (survives restart)
def test_LC7_historical_queue_empty_cannot_erase_active_work(superuser_db, make_verified_user):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.durable import founder_coordination_snapshot
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    from app.db import migration_engine

    owner, a = _seed_assignment(superuser_db, make_verified_user)
    # make the assignment an ACTIVE obligation through the production ingest path
    ingest_lifecycle_event(superuser_db, owner_id=owner, actor="worker",
                           payload={"kind": "assignment_observed", "event_id": f"lc7-seed-{uuid.uuid4().hex[:8]}", "assignment_id": str(a.id), "agent_identity": "cursor"})
    superuser_db.commit()
    # a stale/historical queue-empty report arrives -> must be refused, and must NOT erase the active obligation
    qe = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="worker",
                                payload={"kind": "queue_empty_claim", "event_id": f"lc7-qe-{uuid.uuid4().hex[:8]}", "agent_identity": "mainai"})
    superuser_db.commit()
    print(f"LC7|queue_empty_outcome={qe.get('outcome')} reason={qe.get('reason')}")
    assert qe.get("outcome") == "stale_rejected", "LC-7 a queue-empty claim was accepted while work is active"
    # RESTART: the active obligation must still be there
    with sessionmaker(bind=migration_engine)() as fresh:
        fresh.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(owner)})
        snap = founder_coordination_snapshot(fresh, owner_id=owner)
    obl = snap.get("obligations", [])
    print(f"LC7|obligations_after_restart={len(obl)} queue_empty_flag={snap.get('queue_empty')}")
    assert obl, "LC-7 the active obligation was erased by a historical queue-empty report"
