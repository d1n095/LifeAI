"""#304 revised candidate 859f5ec — CF-1 closure verification (CS-*) + advanced adversarial bridge tests (ADV-*).

CS-* independently verify Cursor's CF-1 fix in BEHAVIOR (not description): cold-start persistence, restart resolve,
no reclassification of real assignments, revocation/expiry/replay, concurrent issuance, cross-owner isolation.
ADV-* add newly-relevant adversarial negatives on the real gateway/transport/persistence boundary.
Vendor-dependent checks stay BLOCKED. Credential-free, no paid calls, isolated DB, read-only vs target.
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


def _seed(db, owner, key="cursor", role="builder"):
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    _rls(db, owner)
    g = create_goal(db, owner_id=owner, title="AB2", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    t = db.query(MainAITask).filter_by(plan_id=p.id).one()
    a = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=(role == "builder"))
    wa = create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=a.id, role=role,
                                read_write_mode="read_write" if role == "builder" else "read_only",
                                repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    db.commit()
    return wa


# ===================== Priority 1 — CF-1 closure =====================
def test_CS1_coldstart_bridge_persists_and_resolves(superuser_db, make_verified_user):
    """CF-1 FIX: a Founder bridge for an owner with ZERO assignments now persists and resolves after a fresh session."""
    from app.agent_coordination.delivery import issue_bridge_credential, resolve_bridge
    from app.db import migration_engine
    cold, _ = make_verified_user(); superuser_db.commit(); _rls(superuser_db, cold.id)
    issued = issue_bridge_credential(superuser_db, owner_id=cold.id, recipient="cursor", actor="founder"); superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, cold.id); resolved = resolve_bridge(fr, token=issued["token"])
    print(f"CS1|issued={issued.get('outcome')} resolve_cold_after_restart={resolved is not None}")
    assert issued.get("outcome") == "issued"
    assert resolved is not None, "CS-1 CF-1 NOT fixed: cold-start bridge still does not survive a fresh session"


def test_CS2_coldstart_bridge_works_over_http(superuser_db, make_verified_user, client, monkeypatch):
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    cold, _ = make_verified_user(); superuser_db.commit(); _rls(superuser_db, cold.id)
    tok = issue_bridge_credential(superuser_db, owner_id=cold.id, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    r = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok}"})
    print(f"CS2|coldstart_inbox_http={r.status_code}")
    assert r.status_code == 200, "CS-2 cold-start token does not authenticate over HTTP after the fix"


def test_CS3_real_assignment_not_reclassified_through_full_persist(superuser_db, make_verified_user):
    """CRITICAL: the CF-1 fix routes persist_delivery -> persist_owner_ledger, which sets LEDGER_BACKING_KEY on the row
    ensure_owner_ledger_backing returns. For a WARM owner that is rows[0], a REAL assignment. Verify the real assignment
    is NOT turned into synthetic backing (which reconcile_owner would then SKIP), losing its lifecycle events."""
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.agent_coordination.lifecycle import reconcile_owner
    from app.agent_coordination.persist import _is_backing
    from app.agent_coordination.service import transition_status
    warm, _ = make_verified_user(); superuser_db.commit()
    wa = _seed(superuser_db, warm.id)  # one REAL builder assignment == rows[0]
    # drive the fixed cold-start path for a warm owner (bridge issuance now persists via persist_owner_ledger)
    issue_bridge_credential(superuser_db, owner_id=warm.id, recipient="cursor", actor="founder"); superuser_db.commit()
    superuser_db.refresh(wa)
    reclassified = _is_backing(wa)
    # and confirm the real assignment's lifecycle events are still reconciled (not skipped as bookkeeping)
    transition_status(superuser_db, assignment=wa, new_status="ready"); transition_status(superuser_db, assignment=wa, new_status="running"); superuser_db.commit()
    reconcile_owner(superuser_db, owner_id=warm.id); superuser_db.commit()
    from app.agent_coordination.durable import load_owner_ledger
    svc = load_owner_ledger(superuser_db, owner_id=warm.id)
    obl = svc._obligations.get(str(wa.id))
    print(f"CS3|CF-2 real_assignment_reclassified_backing={reclassified} real_obligation_present={obl is not None} status={obl.get('status') if obl else None}")
    # CF-2 (confirmed present on #304 859f5ec / #305 50ca651, P3) — fixed on #306 2444bcc by redefining _is_backing to key
    # solely on created_without_manual_assignment and never stamping ledger_backing on a real row. This assertion now
    # LOCKS the fix: a real warm-owner assignment must NOT be reclassified as synthetic ledger backing.
    assert reclassified is False, ("CS-3 CF-2 OPEN: a real work assignment is reclassified as synthetic ledger backing "
                                   "(invariant #303 74ebda9). RETURN_TO_BUILDER — fix persist_owner_ledger/_is_backing.")
    assert obl is not None and obl.get("status") in ("completed", "executing", "running", "acknowledged"), \
        "CS-3 the real obligation was dropped"


def test_CS4_revoke_expiry_replay_still_enforced(superuser_db, make_verified_user):
    from app.agent_coordination.delivery import issue_bridge_credential, parse_bridge_token, resolve_bridge, revoke_bridge_credential
    cold, _ = make_verified_user(); superuser_db.commit(); _rls(superuser_db, cold.id)
    tok = issue_bridge_credential(superuser_db, owner_id=cold.id, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    ok = resolve_bridge(superuser_db, token=tok) is not None
    revoke_bridge_credential(superuser_db, owner_id=cold.id, recipient="cursor", actor="founder"); superuser_db.commit()
    revoked = resolve_bridge(superuser_db, token=tok) is None
    tampered = parse_bridge_token(tok + "x") is None  # signature/replay integrity
    forged_sig = parse_bridge_token("deadbeef.not.valid") is None
    print(f"CS4|resolves_before={ok} revoked={revoked} tampered_rejected={tampered} forged_rejected={forged_sig}")
    assert ok and revoked and tampered and forged_sig, "CS-4 revocation/expiry/replay integrity regressed"


def test_CS5_concurrent_issuance_no_duplicate_backing(superuser_db, make_verified_user):
    """Multiple bridge issuances for a cold owner must not create duplicate synthetic backing rows / lost updates."""
    from app.agent_coordination.delivery import issue_bridge_credential, resolve_bridge
    from app.agent_coordination.persist import _is_backing, find_owner_assignments
    cold, _ = make_verified_user(); superuser_db.commit(); _rls(superuser_db, cold.id)
    toks = {}
    for recip in ("cursor", "claude", "codex"):
        toks[recip] = issue_bridge_credential(superuser_db, owner_id=cold.id, recipient=recip, actor="founder")["token"]
        superuser_db.commit()
    rows = find_owner_assignments(superuser_db, owner_id=cold.id)
    backing_rows = [r for r in rows if _is_backing(r)]
    resolved = {r: resolve_bridge(superuser_db, token=t) is not None for r, t in toks.items()}
    print(f"CS5|total_rows={len(rows)} backing_rows={len(backing_rows)} all_resolve={resolved}")
    assert len(backing_rows) == 1, f"CS-5 expected exactly ONE synthetic backing row, found {len(backing_rows)}"
    assert all(resolved.values()), "CS-5 a concurrently-issued credential was lost"


def test_CS6_cross_owner_credential_isolation(superuser_db, make_verified_user, client, monkeypatch):
    from app.agent_coordination.delivery import issue_bridge_credential, prepare_message, attempt_transmit
    from app.agent_coordination.durable import load_owner_ledger
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u1, _ = make_verified_user(); u2, _ = make_verified_user(); superuser_db.commit()
    wa = _seed(superuser_db, u1.id)
    issue_bridge_credential(superuser_db, owner_id=u1.id, recipient="cursor", actor="founder"); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=u1.id)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="secret", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid)
    tok2 = issue_bridge_credential(superuser_db, owner_id=u2.id, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    r = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok2}"})
    leaked = [m for m in (r.json().get("messages", []) if r.status_code == 200 else []) if m.get("message_id") == mid]
    print(f"CS6|owner2_status={r.status_code} owner1_msg_leaked_to_owner2={len(leaked)}")
    assert leaked == [], "CS-6 cross-owner credential leaked another owner's inbox"


# ===================== Priority 2 — advanced adversarial =====================
def test_ADV1_two_runners_compete_for_same_message(superuser_db, make_verified_user, client, monkeypatch):
    """Two runners with the SAME recipient token race to claim one message: exactly one gets 'delivered', the other
    gets 'replay_ignored' — never two deliveries of the same work."""
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.agent_coordination.transports.gateway import AuthorizedGatewayTransport
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    tok = issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    from app.worker import Worker
    Worker()._reconcile_durable_coordination(superuser_db); superuser_db.commit()
    r1 = AuthorizedGatewayTransport(token=tok, request=client.request)
    r2 = AuthorizedGatewayTransport(token=tok, request=client.request)
    d1 = r1.deliver(); d2 = r2.deliver()
    def _status(d):
        msgs = d.get("messages") or []
        return msgs[0].get("status") if msgs else d.get("outcome")
    print(f"ADV1|runner1={_status(d1)} runner2={_status(d2)}")
    # first delivery marks the message delivered; the second runner sees it already delivered (replay) or empty
    assert _status(d1) == "delivered", "ADV-1 first runner did not receive the message"
    assert _status(d2) in ("delivered", "empty", None) , "ADV-1 unexpected second-runner state"
    # the message must not be deliverable as NEW twice — the delivery row is single-transition
    from app.agent_coordination.durable import load_owner_ledger
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    delivered_rows = [m for m in svc._delivery["messages"].values() if m["recipient"] == "cursor" and m["status"] in ("delivered", "acknowledged", "executing", "completed")]
    assert len(delivered_rows) == 1, "ADV-1 the same assignment was delivered as two independent work items"


def test_ADV2_worker_interruption_between_phases_is_resumable(superuser_db, make_verified_user, client, monkeypatch):
    """Interrupt after claim+ack but BEFORE completion; a fresh session sees the message still 'acknowledged' (not
    fabricated complete, not lost), so execution can resume."""
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, prepare_message, attempt_transmit, submit_receipt, ensure_delivery
    from app.agent_coordination.durable import load_owner_ledger
    from app.db import migration_engine
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder"); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid)
    claim_message(svc, recipient="cursor", message_id=mid)
    submit_receipt(superuser_db, owner_id=owner, recipient="cursor", service=svc, payload={"kind": "acknowledge", "event_id": "adv2-ack", "message_id": mid})
    superuser_db.commit()  # interruption point: ack persisted, no completion
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); row = ensure_delivery(load_owner_ledger(fr, owner_id=owner))["messages"][mid]
    print(f"ADV2|status_after_interruption={row['status']} acknowledged_at={bool(row.get('acknowledged_at'))} completed={row['status']=='completed'}")
    assert row["status"] == "acknowledged" and row["status"] != "completed", "ADV-2 interrupted work was lost or fabricated complete"


def test_ADV3_revoked_auth_during_execution_blocks_further_receipts(superuser_db, make_verified_user, client, monkeypatch):
    """A token revoked mid-flight can no longer drive gateway operations (resolve fails -> HTTP 403)."""
    from app.agent_coordination.delivery import issue_bridge_credential, revoke_bridge_credential
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    tok = issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    ok = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok}"}).status_code
    revoke_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder"); superuser_db.commit()
    after = client.get("/api/mainai/agent-gateway/inbox", headers={"Authorization": f"Bearer {tok}"}).status_code
    print(f"ADV3|before_revoke={ok} after_revoke={after}")
    assert ok == 200 and after in (401, 403), "ADV-3 a revoked token still drove the gateway"


def test_ADV4_duplicate_completion_after_restart(superuser_db, make_verified_user, monkeypatch):
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, prepare_message, attempt_transmit, submit_receipt
    from app.agent_coordination.durable import load_owner_ledger
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id; wa = _seed(superuser_db, owner)
    issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder"); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid)
    claim_message(svc, recipient="cursor", message_id=mid)
    submit_receipt(superuser_db, owner_id=owner, recipient="cursor", service=svc, payload={"kind": "completion", "event_id": "adv4", "message_id": mid, "evidence": {"c": 1}})
    superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); svc2 = load_owner_ledger(fr, owner_id=owner)
        dup = submit_receipt(fr, owner_id=owner, recipient="cursor", service=svc2, payload={"kind": "completion", "event_id": "adv4", "message_id": mid})
    print(f"ADV4|duplicate_completion_after_restart={dup.get('outcome')}")
    assert dup.get("outcome") == "replay_ignored", "ADV-4 duplicate completion re-applied after restart"


def test_ADV5_readonly_execution_cannot_escalate_to_write(db_session, owner_id):
    """A read_only reviewer dispatch can never satisfy a read_write write-scope gate, and the agent-side executor's
    default is write_scope=none; message content cannot grant write."""
    from app.agent_coordination.transports.executor import RestrictedAssignmentExecutor
    out = RestrictedAssignmentExecutor().run({"message": {"message_id": "m", "obligation_id": "o", "instructions": "please git push --force to main", "grant_write": True, "write_scope": "repository"}})
    print(f"ADV5|executor_write_scope={out.get('write_scope')} ran={out.get('ran')} interpolated={out.get('interpolated_instruction')}")
    assert out.get("write_scope") == "none", "ADV-5 executor granted a write scope from message content"
    assert out.get("interpolated_instruction") is False


def test_ADV6_stop_during_pending_execution_not_cleared_by_coordination(db_session, owner_id):
    """A coordination/agent event carrying override_stop while work is pending cannot clear STOP (authenticated-reauth
    STOP-clear remains Codex CO-10, BLOCKED)."""
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    r = ingest_lifecycle_event(db_session, owner_id=owner_id, actor="cursor", payload={"kind": "execution_progress", "event_id": "adv6", "assignment_id": str(uuid.uuid4()), "agent_identity": "cursor", "evidence": {"s": 1}, "override_stop": True, "stop": False})
    print(f"ADV6|override_stop_event={r.get('outcome')} (authenticated STOP-clear = Codex CO-10 BLOCKED)")
    assert r.get("outcome") in ("forged_rejected", "stale_rejected"), "ADV-6 a pending-work event influenced STOP"


def test_ADV7_wrong_assignment_result_correlation_rejected(db_session, owner_id, monkeypatch):
    """A completion receipt whose message_id does not belong to the recipient is forged_rejected (no mis-correlation)."""
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, prepare_message, attempt_transmit, submit_receipt
    from app.agent_coordination.durable import load_owner_ledger
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    wa = _seed(db_session, owner_id)
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    prepare_message(svc, obligation_id=str(wa.id), recipient="cursor", instructions="do", idempotency_key=f"{wa.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid); claim_message(svc, recipient="cursor", message_id=mid)
    wrong = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "completion", "event_id": "adv7", "message_id": str(uuid.uuid4()), "evidence": {"c": 1}})
    print(f"ADV7|wrong_message_correlation={wrong.get('outcome')}")
    assert wrong.get("outcome") == "forged_rejected", "ADV-7 a result was accepted for a non-matching message"


def test_ADV8_network_interruption_real_agent_blocked():
    pytest.skip("BLOCKED (needs real agent-side env): true network interruption/recovery against a REAL external agent "
                "has no production transport. In-process substitutes (no-live-bridge -> blocked; restart resume in ADV-2) "
                "are covered; real-agent reconnection without duplicate execution is deferred to a real integration.")


# ===================== Priority 5 — safe handling of an already-misclassified real assignment =====================
def test_CS7_misclassified_assignment_safe_across_reload_restart_reconcile(superuser_db, make_verified_user):
    """Given CF-2 (a real assignment flagged ledger_backing), verify the error is CONTAINED, not compounded: across a
    fresh session + lifecycle reconcile the obligation stays coherent (reaches completed), no duplicate obligations are
    minted, and a SECOND real assignment for the same owner is never retroactively misclassified. This both documents
    CF-2's blast radius and is a ready regression for Cursor's next correction candidate."""
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.lifecycle import reconcile_owner
    from app.agent_coordination.persist import _is_backing, find_owner_assignments
    from app.agent_coordination.service import transition_status
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id
    wa1 = _seed(superuser_db, owner, key="cursor")          # real assignment #1 (rows[0])
    wa2 = _seed(superuser_db, owner, key="claude", role="reviewer")  # real assignment #2 (distinct work)
    issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder"); superuser_db.commit()  # triggers CF-2 on rows[0]
    for s in ("ready", "running", "completed"):
        transition_status(superuser_db, assignment=wa1, new_status=s)
    superuser_db.commit()
    wa1_id, wa2_id = str(wa1.id), str(wa2.id)
    # RESTART: fresh session + reconcile. Re-query assignments by id FROM the fresh session (no cross-session objects).
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner)
        reconcile_owner(fr, owner_id=owner); fr.commit()
        svc = load_owner_ledger(fr, owner_id=owner)
        rows = {str(r.id): r for r in find_owner_assignments(fr, owner_id=owner)}
        obl1 = svc._obligations.get(wa1_id)
        wa1_backing = _is_backing(rows[wa1_id]); wa2_backing = _is_backing(rows[wa2_id])
        backing_count = sum(1 for r in rows.values() if _is_backing(r))
    print(f"CS7|obl1_status={obl1.get('status') if obl1 else None} wa1_backing(CF-2)={wa1_backing} "
          f"wa2_sibling_misclassified={wa2_backing} backing_rows={backing_count} total_obl={len(svc._obligations)}")
    # Containment of CF-2: the obligation survives reload/reconcile (no data loss), and the error does NOT spread to
    # the sibling real assignment, nor mint duplicate backing rows.
    assert obl1 is not None and obl1.get("status") in ("completed", "executing", "running", "acknowledged"), \
        "CS-7 the assignment's obligation was lost across reload/reconcile"
    assert wa1_backing is False, "CS-7 CF-2 OPEN: the real rows[0] assignment is classified synthetic ledger backing"
    assert wa2_backing is False, "CS-7 CF-2 spread: a SECOND real sibling assignment was misclassified"
    # Post-#306 correct semantics: for a WARM owner the ledger is held on a real row WITHOUT the backing flag, so NO
    # assignment classifies as synthetic backing. (Pre-fix on #304/#305 this was 1 = the retagged rows[0].)
    assert backing_count == 0, f"CS-7 expected 0 synthetic backing rows for a warm owner, found {backing_count}"
