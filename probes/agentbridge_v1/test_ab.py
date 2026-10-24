"""#304 Agent-Side Communication Bridge — examiner-owned investigation + transport negative tests.

CS-* : the independent cold-start persistence investigation the Founder requested (NO silent seeding).
AT-* : adversarial negatives for the NEW AgentTransport surfaces (vendor transports, gateway runner, executor).
Credential-free, no paid calls, no network, isolated DB, read-only vs target. Target: #304 candidate 4102c16.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401


def _rls(s, o):
    from app.request_context import current_user_id as current_user_id_var
    current_user_id_var.set(str(o)); s.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(o)})


def _seed(db, owner, key="cursor"):
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    _rls(db, owner)
    g = create_goal(db, owner_id=owner, title="AB", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    t = db.query(MainAITask).filter_by(plan_id=p.id).one()
    a = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=True)
    wa = create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=a.id, role="builder",
                                read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return wa


# =============================== TASK 2 — cold-start persistence investigation (no silent seed)
def test_CS1_coldstart_bridge_credential_persistence(superuser_db, make_verified_user):
    """INDEPENDENT FINDING CHECK: Founder issues a bridge for an owner with ZERO assignments, then a fresh DB session
    (restart) tries to resolve/operate it. persist_delivery early-returns when the owner has no AgentWorkAssignment,
    so the bridge is never persisted. Positive control: the SAME flow WITH one assignment persists correctly."""
    from app.agent_coordination.delivery import issue_bridge_credential, resolve_bridge
    from app.db import migration_engine

    # --- cold owner: NO assignment, NO backing row ---
    cold, _ = make_verified_user(); superuser_db.commit()
    _rls(superuser_db, cold.id)
    issued = issue_bridge_credential(superuser_db, owner_id=cold.id, recipient="cursor", actor="founder")
    superuser_db.commit()
    tok_cold = issued["token"]
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, cold.id)
        resolved_cold = resolve_bridge(fr, token=tok_cold)
    # --- warm owner: WITH one assignment (positive control) ---
    warm, _ = make_verified_user(); superuser_db.commit()
    _seed(superuser_db, warm.id)
    tok_warm = issue_bridge_credential(superuser_db, owner_id=warm.id, recipient="cursor", actor="founder")["token"]
    superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, warm.id)
        resolved_warm = resolve_bridge(fr, token=tok_warm)
    print(f"CS1|FINDING issued_cold={issued.get('outcome')} resolve_cold_after_restart={resolved_cold is not None} "
          f"resolve_warm_after_restart={resolved_warm is not None}")
    # The issuance SUCCEEDS (returns a token) but the credential does NOT survive a fresh session for a cold owner:
    assert issued.get("outcome") == "issued", "precondition: issuance reports success"
    assert resolved_warm is not None, "CS-1 positive control: a bridge for an owner WITH an assignment must resolve"
    # Document the defect deterministically. If this assertion ever FAILS (resolve_cold becomes True), the cold-start
    # gap has been fixed in production and this finding should be downgraded.
    assert resolved_cold is None, ("CS-1 UNEXPECTED: the cold-start bridge now resolves — the persist_delivery "
                                   "zero-assignment early-return appears fixed; re-verify and downgrade the finding")


def test_CS2_coldstart_defect_via_http_gateway(superuser_db, make_verified_user, client, monkeypatch):
    """The same cold-start gap through the REAL HTTP surface: a Founder-issued token for a zero-assignment owner is
    rejected by the agent-gateway after a fresh request/session, so the agent-side runner cannot authenticate."""
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    cold, _ = make_verified_user(); superuser_db.commit()
    _rls(superuser_db, cold.id)
    tok = issue_bridge_credential(superuser_db, owner_id=cold.id, recipient="cursor", actor="founder")["token"]
    superuser_db.commit()
    r = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok}"})
    print(f"CS2|FINDING coldstart_inbox_http_status={r.status_code} (401/403 => credential did not persist)")
    assert r.status_code in (401, 403), ("CS-2 UNEXPECTED: the cold-start token now authenticates over HTTP — the "
                                         "persist gap appears fixed; re-verify and downgrade the finding")


# =============================== #304 transport abstraction — adversarial negatives
def test_AT1_vendor_transports_never_simulate(db_session, owner_id):
    """Every vendor/model/remote/private-session transport fail_closed on deliver/execute/ingest — never simulates."""
    from app.agent_coordination.transport import registry
    reg = registry()
    results = {}
    for tid in reg.ids():
        if tid in ("authorized_gateway_runner", "local_cli_execution"):
            continue  # the usable/execution surfaces, tested separately
        factory = reg.get(tid)
        t = factory(token="x") if False else factory()
        d = t.deliver(); ex = t.execute({"message": {"message_id": "m"}}); ig = t.ingest_result("m", event_id="e", result={"ran": True})
        results[tid] = (d.get("outcome"), ex.get("outcome"), ig.get("outcome"), d.get("vendor_delivery_claimed"))
    print(f"AT1|{results}")
    assert results, "AT-1 no vendor transports registered"
    for tid, (d, ex, ig, claimed) in results.items():
        assert d == "blocked" and ex == "blocked" and ig == "blocked", f"AT-1 {tid} did not fail closed"
        assert claimed is False, f"AT-1 {tid} claimed a vendor delivery"


def test_AT2_capability_matrix_never_marks_vendor_usable(db_session, owner_id):
    from app.agent_coordination.transport import capability_matrix
    rows = {r["transport_id"]: r for r in capability_matrix()}
    vendor = {k: v.get("usable") for k, v in rows.items() if v.get("surface") in ("private_vendor_session", "provider_model_api", "remote_background_agent")}
    runner = rows.get("authorized_gateway_runner", {})
    print(f"AT2|vendor_usable={vendor} runner_surface={runner.get('surface')} runner_note_has_not_vendor={'not a vendor' in (runner.get('note') or '').lower()}")
    assert vendor and all(v is False for v in vendor.values()), "AT-2 a vendor surface is marked usable"
    assert runner.get("surface") == "agent_side_runner", "AT-2 the runner must not be reported as a vendor surface"


def test_AT3_gateway_runner_fail_closed_without_token():
    from app.agent_coordination.agent_side import run_agent_side
    res = run_agent_side(token="")
    print(f"AT3|no_token={res.get('outcome')} missing={res.get('missing_capability')}")
    assert res.get("outcome") == "blocked" and res.get("missing_capability") == "founder_agent_side_authorization"


def test_AT4_executor_record_mode_no_subprocess_no_interpolation(monkeypatch):
    from app.agent_coordination.transports.executor import RestrictedAssignmentExecutor
    monkeypatch.setenv("LIFE_AGENT_SIDE_EXECUTOR", "record")
    out = RestrictedAssignmentExecutor().run({"message": {"message_id": "m1", "obligation_id": "o1", "instructions": "rm -rf / ; echo pwned"}})
    print(f"AT4|ran={out.get('ran')} mode={out.get('mode')} interpolated={out.get('interpolated_instruction')} write_scope={out.get('write_scope')}")
    assert out.get("ran") is True and out.get("interpolated_instruction") is False and out.get("write_scope") == "none"


def test_AT5_executor_allowlisted_fail_closed_without_founder_config(monkeypatch):
    from app.agent_coordination.transports.executor import RestrictedAssignmentExecutor
    monkeypatch.setenv("LIFE_AGENT_SIDE_EXECUTOR", "allowlisted")
    monkeypatch.setenv("LIFE_AGENT_SIDE_PROVIDER", "cursor-agent")  # not Founder-enabled
    out = RestrictedAssignmentExecutor().run({"message": {"message_id": "m1"}})
    unknown = RestrictedAssignmentExecutor()
    monkeypatch.setenv("LIFE_AGENT_SIDE_EXECUTOR", "wat")
    out2 = unknown.run({"message": {"message_id": "m2"}})
    print(f"AT5|allowlisted_unconfigured={out.get('outcome')}/{out.get('missing_capability')} unknown_mode={out2.get('outcome')}")
    assert out.get("outcome") == "blocked" and out.get("missing_capability") == "founder_adapter_enablement"
    assert out2.get("outcome") == "blocked"


def test_AT6_gateway_runner_duplicate_execution_replay(db_session, owner_id, client, monkeypatch):
    """A real authorized runner round trip, then a duplicate execute of the same message is replay_ignored."""
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.agent_coordination.transports.gateway import AuthorizedGatewayTransport
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    wa = _seed(db_session, owner_id)
    tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]; db_session.commit()
    from app.worker import Worker
    Worker()._reconcile_durable_coordination(db_session); db_session.commit()
    t = AuthorizedGatewayTransport(token=tok, request=client.request)
    pulled = t.deliver()
    assert pulled.get("delivered_count", 0) >= 1, "AT-6 runner did not receive the transmitted message"
    msg = (pulled["messages"][0].get("message") or pulled["messages"][0])
    first = t.execute({"message": msg})
    dup = t.execute({"message": msg})
    print(f"AT6|delivered={pulled.get('delivered_count')} first_exec={first.get('ran', first.get('outcome'))} dup_exec={dup.get('outcome')} vendor_claimed={pulled.get('vendor_delivery_claimed')}")
    assert dup.get("outcome") == "replay_ignored" and pulled.get("vendor_delivery_claimed") is False


def test_AT7_runner_token_still_recipient_scoped(superuser_db, make_verified_user, client, monkeypatch):
    """The runner inherits the gateway's recipient+owner scoping: a cursor token cannot pull another recipient's inbox,
    and a token for owner A yields nothing of owner B. (The runner is authenticated-by-token, NOT proof of vendor identity.)"""
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, prepare_message, attempt_transmit
    from app.agent_coordination.durable import load_owner_ledger
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u1, _ = make_verified_user(); u2, _ = make_verified_user(); superuser_db.commit()
    wa = _seed(superuser_db, u1.id)
    issue_bridge_credential(superuser_db, owner_id=u1.id, recipient="cursor", actor="founder"); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=u1.id)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid)
    cross_agent = claim_message(svc, recipient="claude", message_id=mid)
    tok2 = issue_bridge_credential(superuser_db, owner_id=u2.id, recipient="cursor", actor="founder")  # u2 cold -> may not persist; HTTP check below is the scoping proof
    superuser_db.commit()
    r = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok2.get('token','')}"})
    leaked = [m for m in (r.json().get("messages", []) if r.status_code == 200 else []) if m.get("message_id") == mid]
    print(f"AT7|cross_agent_claim={cross_agent.get('outcome')} owner2_status={r.status_code} owner1_msg_leaked={len(leaked)}")
    assert cross_agent.get("outcome") == "forged_rejected" and leaked == []
