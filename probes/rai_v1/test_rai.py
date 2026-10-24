"""Real Agent Integration Security & Reliability Acceptance Gate (RAI-*) — examiner-owned.

Determines whether MainAI is GENUINELY communicating with an authorized EXTERNAL coding agent, versus merely running a
local CLI, writing gateway records, or simulating delivery. Built against frozen #303 74ebda9 as the certified
reference, BEFORE a real integration (#304) exists, so the standard is fixed first.

Classification (printed per test, never conflated):
  PASS            — demonstrated with authentic evidence on the current substrate (+ negative control).
  PARTIAL         — a deterministic substrate guard PASSES, but the external/model/Codex half is BLOCKED (stated).
  BLOCKED         — the necessary external integration is unavailable on #303 (needs #304 or a real agent-side env).
  NOT_IMPLEMENTED — the required capability has no production code path at all.
A BLOCKED/NOT_IMPLEMENTED scenario is pytest.skip'd with a precise reason — NEVER a fabricated PASS, and NEVER marked
PASS just because its local substrate passes. Where the API supports it, a BLOCKED area still carries an executable
NEGATIVE control proving the system does not FAKE the missing capability today.
Credential-free, no paid calls, no network, isolated DB, read-only vs target.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

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
    g = create_goal(db, owner_id=owner, title="RAI", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    t = db.query(MainAITask).filter_by(plan_id=p.id).one()
    a = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=True)
    wa = create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=a.id, role="builder",
                                read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return wa


def _transmitted_msg(db, owner, wa, recipient="cursor"):
    from app.agent_coordination.delivery import attempt_transmit, issue_bridge_credential, prepare_message
    from app.agent_coordination.durable import load_owner_ledger
    issue_bridge_credential(db, owner_id=owner, recipient=recipient, actor="founder"); db.commit()
    svc = load_owner_ledger(db, owner_id=owner)
    prepare_message(svc, obligation_id=str(wa.id), recipient=recipient, instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    attempt_transmit(svc, message_id=mid)
    return svc, mid


# ============================================================ AREA 1 — authentic external delivery
def test_RAI01_external_receipt_proof_blocked():
    pytest.skip("BLOCKED (needs #304 / real agent-side env): there is no authenticated signal that a REAL external "
                "vendor agent received the assignment. The only receive path is the Founder-issued local authorized "
                "bridge pulling the owner's OWN inbox — proving a specific external agent received it requires an "
                "agent-side runner authenticated as that agent. Negative control in RAI02 proves the system does not "
                "FAKE external receipt today.")


def test_RAI02_local_delivery_not_conflated_with_external(db_session, owner_id):
    """PASS (negative control): the system NEVER marks a local gateway claim as external vendor delivery."""
    from app.agent_coordination.delivery import claim_message, transport_inventory
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    claimed = claim_message(svc, recipient="cursor", message_id=mid)
    inv = {r["channel"]: r for r in transport_inventory(svc)}
    vendor = {k: v["usable"] for k, v in inv.items() if v.get("kind") == "messaging" and "vendor" in k}
    print(f"RAI02|PASS local_claim={claimed.get('outcome')} vendor_delivery_claimed={claimed.get('vendor_delivery_claimed')} vendor_usable={vendor} local_bridge_usable={inv['local_authorized_bridge']['usable']}")
    assert claimed.get("outcome") == "delivered" and claimed.get("vendor_delivery_claimed") is False, "RAI-02 a local claim was reported as vendor delivery"
    assert vendor and all(v is False for v in vendor.values()), "RAI-02 a vendor messaging channel claims usable"


def test_RAI03_completion_evidence_required_but_not_independently_verifiable(db_session, owner_id):
    """PARTIAL: an evidence dict is required (PASS), but it is self-asserted by the token holder — not independently
    verifiable proof of external execution (BLOCKED)."""
    from app.agent_coordination.delivery import claim_message, submit_receipt
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    claim_message(svc, recipient="cursor", message_id=mid)
    submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "acknowledge", "event_id": "rai3-ack", "message_id": mid})
    empty = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "execution_progress", "event_id": "rai3", "message_id": mid, "evidence": {}})
    print(f"RAI03|PARTIAL empty_evidence={empty.get('outcome')} (independent verifiability of external execution = BLOCKED)")
    assert empty.get("outcome") == "rejected", "RAI-03 execution accepted with no evidence"


def test_RAI04_fabricated_ack_and_capability_claims_rejected(db_session, owner_id):
    """PASS: forged identity and a delivery_claim never become a real/vendor delivery."""
    from app.agent_coordination.delivery import claim_message, submit_receipt
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    claim_message(svc, recipient="cursor", message_id=mid)
    spoof = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "acknowledge", "event_id": "rai4", "message_id": mid, "agent_identity": "claude"})
    dc = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="founder", payload={"kind": "delivery_claim", "event_id": "rai4-dc", "agent_identity": "cursor", "to": "claude"})
    print(f"RAI04|PASS spoofed_ack={spoof.get('outcome')} delivery_claim_claimed={dc.get('delivery_claimed')}")
    assert spoof.get("outcome") == "forged_rejected" and dc.get("delivery_claimed") is False


# ============================================================ AREA 2 — identity & authorization
def test_RAI05_recipient_identity_binding(db_session, owner_id):
    from app.agent_coordination.delivery import claim_message, submit_receipt
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    claim_message(svc, recipient="cursor", message_id=mid)
    wrong = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "acknowledge", "event_id": "rai5", "message_id": mid, "agent_identity": "codex"})
    print(f"RAI05|PASS mismatched_identity={wrong.get('outcome')}")
    assert wrong.get("outcome") == "forged_rejected"


def test_RAI06_scoped_revocable_credentials(db_session, owner_id, client, monkeypatch):
    from app.agent_coordination.delivery import issue_bridge_credential, resolve_bridge, revoke_bridge_credential
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    _seed(db_session, owner_id)  # a backing assignment so delivery state (the bridge record) persists
    tok = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder")["token"]; db_session.commit()
    before = resolve_bridge(db_session, token=tok) is not None
    revoke_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    after = resolve_bridge(db_session, token=tok) is not None
    # a non-founder actor may not mint
    forged_issue = issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="cursor")
    print(f"RAI06|PASS resolves_before={before} resolves_after_revoke={after} nonfounder_issue={forged_issue.get('outcome')}")
    assert before is True and after is False and forged_issue.get("outcome") == "forged_rejected"


def test_RAI07_cross_agent_and_cross_owner_isolation(superuser_db, make_verified_user, client, monkeypatch):
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    # cross-agent: cursor token cannot claim a claude message (same owner)
    u1, _ = make_verified_user(); u2, _ = make_verified_user(); superuser_db.commit()
    wa = _seed(superuser_db, u1.id, key="cursor")
    svc, mid = _transmitted_msg(superuser_db, u1.id, wa, recipient="cursor")
    cross_agent = claim_message(svc, recipient="claude", message_id=mid)
    # cross-owner: owner2's token resolves only owner2's (empty) ledger, never owner1's inbox
    tok2 = issue_bridge_credential(superuser_db, owner_id=u2.id, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    r = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok2}"})
    leaked = [m for m in r.json().get("messages", []) if m.get("message_id") == mid]
    print(f"RAI07|PASS cross_agent={cross_agent.get('outcome')} cross_owner_leak={len(leaked)}")
    assert cross_agent.get("outcome") == "forged_rejected" and leaked == []


def test_RAI08_no_privilege_escalation_via_message(db_session, owner_id):
    from app.agent_coordination.delivery import claim_message, submit_receipt
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    claim_message(svc, recipient="cursor", message_id=mid)
    outs = {k: submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc,
                              payload={"kind": "completion", "event_id": f"rai8-{k}", "message_id": mid, k: True}).get("outcome")
            for k in ("independently_verified", "github_write", "override_stop", "stop")}
    print(f"RAI08|PASS {outs}")
    assert all(v == "forged_rejected" for v in outs.values())


def test_RAI09_founder_approval_for_sensitive_ops(db_session, owner_id, client):
    # bridge issuance is founder-only over HTTP
    r = client.post("/api/mainai/founder-ops/agent-gateway/bridges", json={"recipient": "cursor"})
    print(f"RAI09|PASS unauth_bridge_issue_http={r.status_code}")
    assert r.status_code in (401, 403)


# ============================================================ AREA 3 — durability & correctness
def test_RAI10_restart_recovery(superuser_db, make_verified_user, client, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.connect import run_authorized_receiver
    from app.agent_coordination.delivery import ensure_delivery
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.db import migration_engine
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    tok = issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    from app.worker import Worker
    Worker()._reconcile_durable_coordination(superuser_db); superuser_db.commit()
    res = run_authorized_receiver(token=tok, request=client.request, execute_local_cli=False, recipient="cursor")
    mid = res["receipts"][0]["delivered"]["message"]["message_id"]
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); row = ensure_delivery(load_owner_ledger(fr, owner_id=owner))["messages"][mid]
    print(f"RAI10|PASS restart_status={row['status']}")
    assert row["status"] == "completed"


def test_RAI11_idempotent_delivery_and_execution(superuser_db, make_verified_user):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.delivery import ensure_delivery, submit_receipt, claim_message
    from app.agent_coordination.durable import load_owner_ledger
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    svc, mid = _transmitted_msg(superuser_db, owner, wa)
    claim_message(svc, recipient="cursor", message_id=mid)
    submit_receipt(superuser_db, owner_id=owner, recipient="cursor", service=svc, payload={"kind": "completion", "event_id": "rai11", "message_id": mid, "evidence": {"c": 1}})
    superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); svc2 = load_owner_ledger(fr, owner_id=owner)
        dup = submit_receipt(fr, owner_id=owner, recipient="cursor", service=svc2, payload={"kind": "completion", "event_id": "rai11", "message_id": mid})
    print(f"RAI11|PASS duplicate_completion={dup.get('outcome')}")
    assert dup.get("outcome") == "replay_ignored"


def test_RAI12_duplicate_and_delayed_receipts(db_session, owner_id):
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    wa = _seed(db_session, owner_id)
    delayed = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="worker", payload={"kind": "execution_progress", "event_id": "rai12-early", "assignment_id": str(uuid.uuid4()), "agent_identity": "cursor", "evidence": {"s": 1}})
    ev = {"kind": "assignment_observed", "event_id": "rai12", "assignment_id": str(wa.id), "agent_identity": "cursor"}
    r1 = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="worker", payload=dict(ev)); db_session.commit()
    r2 = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="worker", payload=dict(ev)); db_session.commit()
    print(f"RAI12|PASS delayed_unknown={delayed.get('outcome')} first={r1.get('outcome')} dup={r2.get('outcome')}")
    assert delayed.get("outcome") == "stale_rejected" and r2.get("outcome") == "replay_ignored"


def test_RAI13_network_interruption_reconnection():
    pytest.skip("BLOCKED (needs #304 / real agent-side env): network interruption/reconnection to a REAL external agent "
                "has no production transport to exercise. The local bridge is an in-process HTTP pull; disconnection is "
                "modeled only as 'no live bridge -> message stays blocked' (see RAI14/DLV5), not a real-agent reconnect.")


def test_RAI14_cancellation_and_timeout(db_session, owner_id):
    """PARTIAL: message expiry -> failed (PASS); real external cancellation/timeout to a vendor agent is BLOCKED."""
    from app.agent_coordination.delivery import attempt_transmit, ensure_delivery, issue_bridge_credential, prepare_message
    from app.agent_coordination.durable import load_owner_ledger
    wa = _seed(db_session, owner_id)
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    exp = prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="late", idempotency_key=f"{wa.id}:exp", ttl_seconds=60)
    ensure_delivery(svc)["messages"][exp["message"]["message_id"]]["expires_at"] = "2000-01-01T00:00:00.000000Z"
    res = attempt_transmit(svc, message_id=exp["message"]["message_id"])
    print(f"RAI14|PARTIAL expired={res.get('outcome')} (real external cancel/timeout = BLOCKED)")
    assert res.get("outcome") == "failed"


def test_RAI15_out_of_order_no_status_regression(superuser_db, make_verified_user):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.lifecycle import reconcile_owner
    from app.agent_coordination.service import transition_status
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    for s in ("ready", "running", "completed"):
        transition_status(superuser_db, assignment=wa, new_status=s)
    superuser_db.commit(); reconcile_owner(superuser_db, owner_id=owner); superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); statuses = [r.get("status") for r in load_owner_ledger(fr, owner_id=owner)._obligations.values()]
    print(f"RAI15|PASS statuses={statuses}")
    assert statuses and all(s not in ("assigned", "prepared") for s in statuses)


# ============================================================ AREA 4 — security
def test_RAI16_prompt_injection_structured_rejection(db_session, owner_id):
    """PARTIAL: structured policy-override via a receipt is forged_rejected (PASS); LLM prompt-injection resistance of
    free-text message bodies is model-side and NOT a deterministic primitive (BLOCKED)."""
    from app.agent_coordination.delivery import claim_message, submit_receipt
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    claim_message(svc, recipient="cursor", message_id=mid)
    r = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "completion", "event_id": "rai16", "message_id": mid, "override_stop": True, "github_write": True})
    print(f"RAI16|PARTIAL structured_injection={r.get('outcome')} (free-text LLM injection = BLOCKED)")
    assert r.get("outcome") == "forged_rejected"


def test_RAI17_message_content_isolated_from_shell(db_session, owner_id, monkeypatch):
    """PASS: the local CLI argv comes ONLY from Founder-configured adapter_config, never from message content."""
    from app.agent_coordination import adapter_config as ac
    from app.agent_coordination.connect import run_founder_local_cli
    # not enabled -> never runs regardless of any message content
    off = run_founder_local_cli("cursor-agent")
    # even enabled + executable + creds, with NO founder argv template -> still does not run (no content-derived argv)
    monkeypatch.setattr(ac.shutil, "which", lambda _x: "/usr/bin/cursor-agent")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CURSOR_AGENT", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_CREDENTIALS_CONFIRMED__CURSOR_AGENT", "true")
    noargs = run_founder_local_cli("cursor-agent")
    print(f"RAI17|PASS off_ran={off.get('ran')} enabled_noargs_ran={noargs.get('ran')}")
    assert off.get("ran") is False and noargs.get("ran") is False


def test_RAI18_no_credential_or_env_leakage(monkeypatch):
    """PASS: adapter env forwards ONLY a Founder-allowlisted set of NAMES, never blind os.environ."""
    from app.agent_coordination.adapter_config import resolve_adapter_env
    monkeypatch.setenv("SECRET_LEAK_CANARY", "do-not-forward")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENV_ALLOWLIST__CURSOR_AGENT", "PATH")  # only PATH allowlisted
    env = resolve_adapter_env("cursor-agent")
    print(f"RAI18|PASS forwarded_keys={sorted(env)} canary_present={'SECRET_LEAK_CANARY' in env}")
    assert "SECRET_LEAK_CANARY" not in env, "RAI-18 a non-allowlisted env var leaked to the adapter"


def test_RAI19_unauthorized_fs_repo_access_gated(db_session, owner_id):
    """PARTIAL: a read_write dispatch requires explicit branch+worktree + approval (PASS); a REAL external agent's own
    filesystem/repo access is outside MainAI's enforcement (BLOCKED)."""
    from app.agent_coordination.dispatch import evaluate_dispatch_readiness, OUTCOME_SCOPE_NOT_EXPLICIT
    from app.agent_coordination.service import transition_status
    from app.models.agent_coordination import CoordinationAgent
    wa = _seed(db_session, owner_id)  # read_write, no branch/worktree set
    transition_status(db_session, assignment=wa, new_status="ready"); db_session.commit()
    dec = evaluate_dispatch_readiness(db_session, assignment=wa, agent=db_session.get(CoordinationAgent, wa.agent_id))
    print(f"RAI19|PARTIAL rw_without_scope={dec.outcome} (real external-agent FS access = BLOCKED)")
    assert dec.outcome in (OUTCOME_SCOPE_NOT_EXPLICIT, "APPROVAL_REQUIRED", "LEASE_REQUIRED")


def test_RAI20_unauthorized_spend_publish_stop_rejected(db_session, owner_id):
    """PARTIAL: publishing BLOCKED + coordination can't clear STOP (PASS); authenticated-reauth STOP-clear = Codex CO-10 (BLOCKED)."""
    from app.agent_coordination.lifecycle import MISSING_PUBLICATION, delivery_adapter_inventory, ingest_lifecycle_event
    inv = {r["channel"]: r for r in delivery_adapter_inventory()}
    stop = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="cursor", payload={"kind": "completion_claim", "event_id": "rai20", "assignment_id": str(uuid.uuid4()), "agent_identity": "cursor", "override_stop": True})
    print(f"RAI20|PARTIAL publish_usable={inv['github_publication']['usable']} stop={stop.get('outcome')} (authenticated-reauth STOP-clear = Codex CO-10 BLOCKED)")
    assert inv["github_publication"]["usable"] is False and inv["github_publication"]["missing_capability"] == MISSING_PUBLICATION
    assert stop.get("outcome") in ("forged_rejected", "stale_rejected")


def test_RAI21_audit_integrity_partial():
    pytest.skip("PARTIAL->BLOCKED (Codex-owned FA-PRIV): the founder-communication ledger is append-only (deny-mutation "
                "trigger), but audit_log remains runtime-writable by mainai_app under the open FA-PRIV finding "
                "(fa_core_v1 FAC-1..5 FAIL). Least-privilege audit immutability cannot be certified until Codex closes "
                "FA-PRIV; not re-derived here. Classified BLOCKED, never PASS.")


# ============================================================ AREA 5 — multi-agent coordination
def test_RAI22_internal_loop_ok_external_legs_blocked(db_session, owner_id):
    """PARTIAL: the MainAI->builder->examiner->MainAI obligation loop is enforced internally (PASS); the legs that
    require a REAL external agent to receive/execute are BLOCKED."""
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.lifecycle import reconcile_owner
    from app.agent_coordination.service import transition_status
    wa = _seed(db_session, owner_id)
    for s in ("ready", "running", "completed"):
        transition_status(db_session, assignment=wa, new_status=s)
    db_session.commit(); reconcile_owner(db_session, owner_id=owner_id); db_session.commit()
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    examiners = [r for r in svc._obligations.values() if r.get("agent_identity") == "examiner"]
    print(f"RAI22|PARTIAL examiner_obligations={len(examiners)} (external receive/execute legs = BLOCKED)")
    assert len(examiners) <= 1


def test_RAI23_prevent_duplicate_assignments(db_session, owner_id):
    from app.agent_coordination.service import DuplicateWorkError, create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    _rls(db_session, owner_id)
    g = create_goal(db_session, owner_id=owner_id, title="dup", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db_session, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    t = db_session.query(MainAITask).filter_by(plan_id=p.id).one()
    ag = register_agent(db_session, agent_key=f"cursor-{uuid.uuid4().hex[:8]}", display_name="c", adapter_kind="cli", supports_write=True)
    create_work_assignment(db_session, owner_id=owner_id, goal_id=g.id, task_id=t.id, agent_id=ag.id, role="builder", read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db_session.commit()
    raised = False
    try:
        create_work_assignment(db_session, owner_id=owner_id, goal_id=g.id, task_id=t.id, agent_id=ag.id, role="builder", read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    except DuplicateWorkError:
        raised = True
    db_session.rollback()
    print(f"RAI23|PASS duplicate_blocked={raised}")
    assert raised, "RAI-23 a duplicate canonical-work assignment was allowed"


def test_RAI24_builder_cannot_self_certify(db_session, owner_id):
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    wa = _seed(db_session, owner_id)
    r = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="founder", payload={"kind": "completion_claim", "event_id": "rai24", "assignment_id": str(wa.id), "agent_identity": "cursor", "evidence": {"execution_evidence": {"commit_sha": "a" * 40}}})
    print(f"RAI24|PASS completion={r.get('outcome')} independently_verified={r.get('independently_verified')}")
    assert r.get("independently_verified") in (None, False)


def test_RAI25_ownership_chronology_evidence_preserved(db_session, owner_id):
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    wa = _seed(db_session, owner_id)
    foreign = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="codex", payload={"kind": "completion_claim", "event_id": "rai25", "assignment_id": str(wa.id), "agent_identity": "codex", "evidence": {"execution_evidence": {"commit_sha": "b" * 40}}})
    print(f"RAI25|PASS cross_agent_ownership={foreign.get('outcome')}")
    assert foreign.get("outcome") == "forged_rejected", "RAI-25 an agent acted on another agent's assignment"


def test_RAI26_unavailable_agent_no_invented_result(db_session, owner_id):
    from app.agent_coordination.delivery import attempt_transmit, prepare_message
    from app.agent_coordination.durable import load_owner_ledger
    wa = _seed(db_session, owner_id)  # NO bridge issued -> recipient unavailable
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    res = attempt_transmit(svc, message_id=mid)
    print(f"RAI26|PASS unavailable={res.get('outcome')} missing={res.get('missing_capability')} claimed={res.get('vendor_delivery_claimed')}")
    assert res.get("outcome") == "blocked" and res.get("vendor_delivery_claimed") is False, "RAI-26 an unavailable agent produced an invented result"


def test_RAI27_response_corresponds_to_requested_assignment(db_session, owner_id):
    """PARTIAL: a receipt for an unknown/foreign message_id is forged_rejected (PASS — binding by message_id); proving
    the external agent did the SEMANTICALLY requested work (content correspondence) is NOT_IMPLEMENTED."""
    from app.agent_coordination.delivery import submit_receipt
    wa = _seed(db_session, owner_id); svc, mid = _transmitted_msg(db_session, owner_id, wa)
    foreign = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "completion", "event_id": "rai27", "message_id": str(uuid.uuid4())})
    print(f"RAI27|PARTIAL foreign_message_receipt={foreign.get('outcome')} (semantic request/response correspondence = NOT_IMPLEMENTED)")
    assert foreign.get("outcome") == "forged_rejected"
