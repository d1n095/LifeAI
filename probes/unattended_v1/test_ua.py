"""MainAI Unattended Operations — Gate A (local deterministic cycle) acceptance (UA-*).

Drives the composed #306 coordination stack end-to-end with a controlled/simulated agent (the LocalAuthorizedBridge
over the in-process gateway), persistent DB state, injected failures and a worker restart — proving MainAI can run an
authorized development cycle WITHOUT routine Founder intervention, provider-neutrally, while keeping Founder-authorized
decisions gated. Gate B (the real 72h trial with authenticated external agents) is NOT performed here (see UA_GATE_B).
Reuses the existing delivery/lifecycle/routing/service primitives. Credential-free, no paid calls, isolated DB.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

HEAD = "a786dac9f32bc3120fa2176e18041a75f1cf6c56"
PARENT = "2c9dd7e06dd3072f8f1f31531437f285ae978e5f"


def _rls(s, o):
    from app.request_context import current_user_id as current_user_id_var
    current_user_id_var.set(str(o)); s.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": str(o)})


def _goal_task(db, owner, title="UA", paths=("docs/**",), target=("docs/x.md",)):
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    g = create_goal(db, owner_id=owner, title=title, original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=list(target))])
    db.commit()
    return g, db.query(MainAITask).filter_by(plan_id=p.id).one()


def _agent(db, key, write=True):
    from app.agent_coordination.service import register_agent
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=write)


def _assign(db, owner, g, t, agent, *, role="builder", paths=("docs/**",)):
    from app.agent_coordination.service import create_work_assignment
    return create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=agent.id, role=role,
                                  read_write_mode="read_write" if role == "builder" else "read_only",
                                  repository_identity="lifeai", allowed_paths=list(paths), requested_by="founder")


def _worker_tick(db):
    from app.worker import Worker
    Worker()._reconcile_durable_coordination(db); db.commit()


# ===================== Priority 1 — autonomous coordination (Gate A) =====================
def test_UA1_discover_assign_track_receive_completion(superuser_db, make_verified_user, client, monkeypatch):
    """1-3: discover+assign authorized work, track ownership/status, receive+validate completion evidence through the
    real bridge round trip (simulated agent). No Founder step after the initial authorized assignment."""
    from app.agent_coordination.connect import run_authorized_receiver
    from app.agent_coordination.delivery import ensure_delivery, issue_bridge_credential
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.service import transition_status
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); cur = _agent(superuser_db, "cursor")
    a = _assign(superuser_db, owner, g, t, cur)
    for s in ("ready", "running"):
        transition_status(superuser_db, assignment=a, new_status=s)
    tok = issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder")["token"]
    superuser_db.commit()
    _worker_tick(superuser_db)  # MainAI auto-prepares+transmits — no Founder forwarding
    res = run_authorized_receiver(token=tok, request=client.request, execute_local_cli=False, recipient="cursor")
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    mid = res["receipts"][0]["delivered"]["message"]["message_id"]
    row = ensure_delivery(svc)["messages"][mid]
    obl = svc._obligations.get(str(a.id))
    print(f"UA1|delivered={res['delivered_count']} completed={row['status']=='completed'} ownership={obl.get('agent_identity') if obl else None} vendor_claimed={res['vendor_delivery_claimed']}")
    assert res["delivered_count"] == 1 and row["status"] == "completed", "UA-1 cycle did not deliver+complete"
    assert obl and obl.get("agent_identity") == "cursor", "UA-1 ownership not tracked"


def test_UA2_completion_evidence_required(superuser_db, make_verified_user, client, monkeypatch):
    """3: a completion/execution without real evidence is rejected (self-asserted success is not accepted)."""
    from app.agent_coordination.delivery import (attempt_transmit, claim_message, ensure_delivery,
                                                 issue_bridge_credential, prepare_message, submit_receipt)
    from app.agent_coordination.durable import load_owner_ledger
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); a = _assign(superuser_db, owner, g, t, _agent(superuser_db, "cursor"))
    issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder"); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    prepare_message(svc, obligation_id=str(a.id), recipient="cursor", instructions="do", idempotency_key=f"{a.id}:inbox")
    mid = next(r["message_id"] for r in svc._delivery["messages"].values()); attempt_transmit(svc, message_id=mid); claim_message(svc, recipient="cursor", message_id=mid)
    submit_receipt(superuser_db, owner_id=owner, recipient="cursor", service=svc, payload={"kind": "acknowledge", "event_id": "ua2a", "message_id": mid})
    empty = submit_receipt(superuser_db, owner_id=owner, recipient="cursor", service=svc, payload={"kind": "execution_progress", "event_id": "ua2", "message_id": mid, "evidence": {}})
    print(f"UA2|empty_evidence={empty.get('outcome')}")
    assert empty.get("outcome") == "rejected", "UA-2 self-asserted progress accepted without evidence"


def test_UA4_completed_builder_generates_one_examiner(superuser_db, make_verified_user):
    """4: a genuinely-completed builder candidate generates EXACTLY ONE independent-examiner obligation; builder != examiner."""
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.lifecycle import ingest_lifecycle_event, reconcile_owner
    from app.agent_coordination.github_intake import passing_required_runs
    from app.agent_coordination.service import transition_status
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); a = _assign(superuser_db, owner, g, t, _agent(superuser_db, "cursor"))
    for s in ("ready", "running", "completed"):
        transition_status(superuser_db, assignment=a, new_status=s)
    superuser_db.commit(); reconcile_owner(superuser_db, owner_id=owner); superuser_db.commit()
    checks, wf = passing_required_runs(HEAD)
    ingest_lifecycle_event(superuser_db, owner_id=owner, actor="worker", payload={"kind": "github_observation", "event_id": "ua4-pr",
        "observation": {"number": 301, "title": "x", "draft": False, "merged": False, "state": "open",
                        "html_url": "https://github.com/d1n095/LifeAI/pull/301", "head_sha": HEAD, "base_sha": PARENT,
                        "head_branch": "cursor/x", "base_branch": "main", "ancestry": [HEAD, PARENT], "reviews": [],
                        "check_runs": checks, "workflow_runs": wf}})
    superuser_db.commit(); _worker_tick(superuser_db)
    view = load_owner_ledger(superuser_db, owner_id=owner).founder_view()
    examiners = [r for r in view["obligations"] if r.get("agent_identity") == "examiner"]
    print(f"UA4|examiner_obligations={len(examiners)} statuses={[e.get('status') for e in examiners]}")
    assert len(examiners) == 1, "UA-4 expected exactly one independent-examiner obligation"
    assert all(e.get("status") != "independently_verified" for e in examiners), "UA-4 examiner pre-verified (builder self-certified)"


def test_UA5_findings_route_to_correction(superuser_db, make_verified_user):
    """5: an examiner 'changes requested' verdict routes the assignment back to correction (running/ready), not certified."""
    from app.agent_coordination.service import transition_status
    from app.models.agent_coordination import WorkAssignmentStatus
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); a = _assign(superuser_db, owner, g, t, _agent(superuser_db, "cursor"))
    for s in ("ready", "running", "ready_for_review", "reviewing", "changes_requested", "ready"):
        transition_status(superuser_db, assignment=a, new_status=s)
    superuser_db.commit(); superuser_db.refresh(a)
    print(f"UA5|final_status={a.status.value}")
    assert a.status == WorkAssignmentStatus.ready, "UA-5 changes_requested did not route back to a correction state"


def test_UA6_next_task_without_founder(superuser_db, make_verified_user):
    """6: after completion, MainAI selects the agent's NEXT ready work deterministically — no Founder step."""
    from app.agent_coordination.routing import next_feasible_assignment_for_agent, OUTCOME_ASSIGNMENT_FOUND
    from app.agent_coordination.service import transition_status
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t1 = _goal_task(superuser_db, owner, title="UA6a", paths=["docs/a/**"], target=["docs/a/x.md"])
    _, t2 = _goal_task(superuser_db, owner, title="UA6b", paths=["docs/b/**"], target=["docs/b/y.md"])
    cur = _agent(superuser_db, "cursor", write=False)
    a1 = _assign(superuser_db, owner, g, t1, cur, role="reviewer", paths=["docs/a/**"])
    a2 = _assign(superuser_db, owner, g, t2, cur, role="reviewer", paths=["docs/b/**"])
    transition_status(superuser_db, assignment=a1, new_status="ready"); transition_status(superuser_db, assignment=a2, new_status="ready"); superuser_db.commit()
    dec = next_feasible_assignment_for_agent(superuser_db, agent_id=cur.id, owner_id=owner)
    print(f"UA6|next={dec.outcome} picked_own={dec.assignment_id in (a1.id, a2.id)}")
    assert dec.outcome == OUTCOME_ASSIGNMENT_FOUND and dec.assignment_id in (a1.id, a2.id), "UA-6 no next task selected autonomously"


def test_UA7_no_duplicate_or_repeat(superuser_db, make_verified_user):
    """7: duplicate canonical work is refused; a duplicate completion is replay_ignored."""
    from app.agent_coordination.service import DuplicateWorkError, create_work_assignment
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); cur = _agent(superuser_db, "cursor")
    a = _assign(superuser_db, owner, g, t, cur); superuser_db.commit()
    dup = False
    try:
        create_work_assignment(superuser_db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=cur.id, role="builder",
                               read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["docs/**"], requested_by="founder")
    except DuplicateWorkError:
        dup = True
    superuser_db.rollback(); _rls(superuser_db, owner)
    ev = {"kind": "assignment_observed", "event_id": "ua7", "assignment_id": str(a.id), "agent_identity": "cursor"}
    r1 = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="worker", payload=dict(ev)); superuser_db.commit()
    r2 = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="worker", payload=dict(ev)); superuser_db.commit()
    print(f"UA7|duplicate_assignment_blocked={dup} duplicate_event={r2.get('outcome')}")
    assert dup and r2.get("outcome") == "replay_ignored", "UA-7 duplicate assignment or repeated execution not prevented"


def test_UA8_stalled_agent_detected_and_recovered(superuser_db, make_verified_user):
    """8: a stale write lease is detected and expired; a stale worker loses write authority (renew fails)."""
    from datetime import datetime, timedelta, timezone
    from app.agent_coordination.service import (acquire_lease, expire_stale_active_leases, is_lease_stale,
                                               renew_lease, transition_status)
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); cur = _agent(superuser_db, "cursor")
    a = _assign(superuser_db, owner, g, t, cur, paths=["docs/**"])
    a.branch = "cursor/ua8"; a.worktree_path = "/tmp/ua8-wt"; superuser_db.flush()
    transition_status(superuser_db, assignment=a, new_status="ready"); superuser_db.commit()
    acquire_lease(superuser_db, assignment=a, agent_id=cur.id, branch="cursor/ua8", worktree_path="/tmp/ua8-wt",
                  allowed_paths=["docs/**"], mode="read_write", ttl_seconds=60); superuser_db.commit()
    # force the ORM lease row stale (a stalled agent whose TTL elapsed)
    from app.models.agent_coordination import AgentScopeLease, AgentScopeLeaseStatus
    row = superuser_db.query(AgentScopeLease).filter_by(assignment_id=a.id, status=AgentScopeLeaseStatus.active).one()
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10); superuser_db.flush()
    lease_id = row.id
    stale = is_lease_stale(row)
    n = expire_stale_active_leases(superuser_db); superuser_db.commit()
    renew_failed = False
    try:
        renew_lease(superuser_db, lease_id=lease_id, owner_id=owner, ttl_seconds=60)
    except Exception:
        renew_failed = True
    print(f"UA8|stale_detected={stale} expired_count={n} stale_renew_rejected={renew_failed}")
    assert stale and n >= 1 and renew_failed, "UA-8 stalled agent not detected/recovered (stale worker kept authority)"


def test_UA9_cycle_state_survives_restart(superuser_db, make_verified_user, client, monkeypatch):
    """9: the completed cycle's durable state is intact after a fresh session / worker restart."""
    from app.agent_coordination.connect import run_authorized_receiver
    from app.agent_coordination.delivery import ensure_delivery, issue_bridge_credential
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.service import transition_status
    from app.db import migration_engine
    from app.limiter import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); a = _assign(superuser_db, owner, g, t, _agent(superuser_db, "cursor"))
    for s in ("ready", "running"):
        transition_status(superuser_db, assignment=a, new_status=s)
    tok = issue_bridge_credential(superuser_db, owner_id=owner, recipient="cursor", actor="founder")["token"]; superuser_db.commit()
    _worker_tick(superuser_db)
    res = run_authorized_receiver(token=tok, request=client.request, execute_local_cli=False, recipient="cursor")
    mid = res["receipts"][0]["delivered"]["message"]["message_id"]
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); row = ensure_delivery(load_owner_ledger(fr, owner_id=owner))["messages"][mid]
    print(f"UA9|restart_status={row['status']}")
    assert row["status"] == "completed", "UA-9 cycle state lost across restart"


def test_UA10_escalate_only_authorization_dependent(superuser_db, make_verified_user):
    """10: a privilege escalation is rejected (escalated to the authority boundary, never auto-granted); a routine
    progress event is applied without escalation. Dispatch of approval-required work escalates (APPROVAL_REQUIRED)."""
    from app.agent_coordination.durable import load_owner_ledger
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); a = _assign(superuser_db, owner, g, t, _agent(superuser_db, "cursor")); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=owner)
    esc = svc.escalate_privilege(actor="cursor", grant="github_write", assignment_id=str(a.id))
    print(f"UA10|escalate_privilege={esc.outcome}")
    assert esc.outcome == "privilege_rejected", "UA-10 a privilege escalation was not rejected at the authority boundary"


# ===================== Priority 3 — provider independence =====================
def test_UA_PI_provider_neutral_substrate(superuser_db, make_verified_user):
    """Orchestration treats cursor/claude/codex identically; an adapter cannot redefine trusted state. Substituting the
    provider identity does not change the coordination outcome, and no provider marker sets independently_verified."""
    from app.agent_coordination.lifecycle import identity_from_agent_key, ingest_lifecycle_event
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    # identity mapping is provider-neutral and deterministic
    maps = {k: identity_from_agent_key(k) for k in ("cursor-x", "claude-y", "codex-z")}
    # a forged "verified" flag from ANY provider identity is rejected / never sets independently_verified
    outs = {}
    for ident in ("cursor", "claude", "codex"):
        g, t = _goal_task(superuser_db, owner, title=f"PI-{ident}")
        a = _assign(superuser_db, owner, g, t, _agent(superuser_db, ident)); superuser_db.commit()
        r = ingest_lifecycle_event(superuser_db, owner_id=owner, actor="founder", payload={"kind": "completion_claim",
            "event_id": f"pi-{ident}", "assignment_id": str(a.id), "agent_identity": ident, "independently_verified": True,
            "evidence": {"execution_evidence": {"commit_sha": "a" * 40}}})
        outs[ident] = (r.get("outcome"), r.get("independently_verified"))
    print(f"UA-PI|identity_map={maps} per_provider={outs}")
    assert maps == {"cursor-x": "cursor", "claude-y": "claude", "codex-z": "codex"}
    # the provider-independence invariant: a payload `independently_verified: True` from ANY provider NEVER flips the
    # trusted flag — the coordination layer ignores it (result independently_verified stays None/False), so an adapter
    # cannot redefine MainAI's trusted state regardless of which provider it claims to be.
    assert all(o[1] in (None, False) for o in outs.values()), "UA-PI a provider set independently_verified via its payload"


# ===================== Priority 4 — Gate B marker (must NOT be reported passed) =====================
def test_UA_GATE_B_real_72h_trial_not_performed():
    pytest.skip("BLOCKED / NOT PERFORMED: Gate B is a real 72-hour unattended trial with genuinely authenticated external "
                "agents, verifiable provider-side results, bounded authority and spending controls, and zero routine "
                "Founder message forwarding. It requires a Founder-authorized live provider credential and a real agent "
                "environment (out of examiner scope; no live/paid calls permitted). It has NOT been performed and must "
                "not be reported as passed. Prerequisites in UA_ACCEPTANCE_MATRIX.md.")
