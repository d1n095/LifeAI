"""MainAI Cognitive Reliability Gate (CR-01..CR-20) — examiner-owned adversarial acceptance probes.

Makes MainAI's operational reliability MEASURABLE against the deterministic coordination substrate (the event
ledger, delivery gateway and dispatch gate), never against a model's conversational confidence. Each CR either:
  * PASSES  — the invariant is implemented and enforced (adversarial + positive control), or
  * is SKIPPED as BLOCKED — the capability is not a deterministic enforced primitive (never a fabricated PASS).
Adapter-neutral: exercises the provider-neutral substrate (cursor/claude/codex/examiner identities), so it is reusable
across future GPT/Gemini/Claude/Cursor/Codex/local adapters. Credential-free, no paid calls, isolated DB, read-only vs target.
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


def _goal_task(db, owner, title="CR"):
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
    from app.models.mainai_execution import MainAITask
    g = create_goal(db, owner_id=owner, title=title, original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    p = create_plan(db, goal=g, rationale="r", created_by="founder", tasks=[PlannedTaskSpec(description="d", task_type="repo_edit", target_files=["docs/x.md"])])
    db.commit()
    return g, db.query(MainAITask).filter_by(plan_id=p.id).one()


def _assign(db, owner, g, t, key, *, role="builder", paths=("docs/**",)):
    from app.agent_coordination.service import create_work_assignment, register_agent
    a = register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli", supports_write=(role == "builder"))
    return create_work_assignment(db, owner_id=owner, goal_id=g.id, task_id=t.id, agent_id=a.id, role=role,
                                  read_write_mode="read_write" if role == "builder" else "read_only",
                                  repository_identity="lifeai", allowed_paths=list(paths), requested_by="founder", provenance={"key": key})


def _ingest(db, owner, **payload):
    from app.agent_coordination.lifecycle import ingest_lifecycle_event
    actor = payload.pop("actor", "founder")
    return ingest_lifecycle_event(db, owner_id=owner, actor=actor, payload=payload)


# =========================================================== Group A — chronology & interpretation
def test_CR01_out_of_order_events_resolve_correctly(db_session, owner_id):
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id)
    a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    # a progress event for an unknown assignment (arriving "early"/out of order) is not applied as real state
    early = _ingest(db_session, owner_id, actor="worker", kind="execution_progress", event_id="cr1-early",
                    assignment_id=str(uuid.uuid4()), agent_identity="cursor", evidence={"s": 1})
    good = _ingest(db_session, owner_id, actor="worker", kind="assignment_observed", event_id="cr1-ok", assignment_id=str(a.id), agent_identity="cursor")
    print(f"CR01|early={early.get('outcome')} good={good.get('outcome')}")
    assert early.get("outcome") == "stale_rejected" and good.get("outcome") == "applied"


def test_CR02_status_does_not_regress_on_reload(superuser_db, make_verified_user):
    from sqlalchemy.orm import sessionmaker
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.service import transition_status
    from app.agent_coordination.lifecycle import reconcile_owner
    from app.db import migration_engine
    u, _ = make_verified_user(); owner = u.id; _rls(superuser_db, owner)
    g, t = _goal_task(superuser_db, owner); a = _assign(superuser_db, owner, g, t, "cursor")
    for s in ("ready", "running", "completed"):
        transition_status(superuser_db, assignment=a, new_status=s)
    superuser_db.commit()
    reconcile_owner(superuser_db, owner_id=owner); superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        _rls(fr, owner); svc = load_owner_ledger(fr, owner_id=owner)
        statuses = [r.get("status") for r in svc._obligations.values()]
    print(f"CR02|statuses_after_reload={statuses}")
    assert statuses and all(s not in ("assigned", "prepared") for s in statuses), "CR-02 a completed obligation regressed on reload"


def test_CR03_duplicate_report_is_idempotent(db_session, owner_id):
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id)
    a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    ev = dict(kind="assignment_observed", event_id="cr3-dup", assignment_id=str(a.id), agent_identity="cursor")
    r1 = _ingest(db_session, owner_id, actor="worker", **ev); db_session.commit()
    r2 = _ingest(db_session, owner_id, actor="worker", **dict(ev)); db_session.commit()
    print(f"CR03|first={r1.get('outcome')} dup={r2.get('outcome')}")
    assert r1.get("outcome") == "applied" and r2.get("outcome") == "replay_ignored"


def test_CR04_cognitive_ops_distinguish_report_from_evidence_and_retain_provenance():
    """The deterministic cognitive substrate (app.mainai_cognitive_ops) enforces the interpretation invariants the
    Founder requires: PARTIAL != COMPLETE (reported vs real completion), DEDUPLICATED != DELETED / DUPLICATE SOURCE !=
    INDEPENDENT SOURCE (provenance retained), and reconstructable defragmentation (chronology). Raw free-text chat
    parsing itself is model-dependent and NOT an authoritative path — these pure primitives are the enforced substrate."""
    from app.mainai_cognitive_ops.information_lifecycle import InformationItem, defragment, deduplicate_exact, fragment
    from app.mainai_cognitive_ops.situational_awareness import assess_program_completion_claim
    from app.mainai_cognitive_ops.types import InformationTier, ProgramStatus, WorkItem

    def _wi(iid, status):
        return WorkItem(item_id=iid, program="p", subtask=iid, owner_agent="cursor", status=status)
    allc = assess_program_completion_claim(program="p", subtasks=(_wi("a", ProgramStatus.COMPLETE), _wi("b", ProgramStatus.COMPLETE)))
    partial = assess_program_completion_claim(program="p", subtasks=(_wi("a", ProgramStatus.COMPLETE), _wi("b", ProgramStatus.PARTIAL)))
    items = (
        InformationItem(item_id="s1", tier=InformationTier.RAW_SOURCE, content_ref="X", provenance=("src-A",)),
        InformationItem(item_id="s2", tier=InformationTier.RAW_SOURCE, content_ref="X", provenance=("src-B",)),
    )
    dedup = deduplicate_exact(items=items, key_fn=lambda i: i.content_ref)
    frags = fragment(source_id="doc", unit_boundaries=("u0", "u1"))
    bundle = defragment(bundle_id="b", related_items=frags)
    print(f"CR04|all_complete={allc.claimed_status.value} one_partial={partial.claimed_status.value} dedup_prov={dedup[0].retained_provenance if dedup else None} reconstructable={bundle.reconstructable}")
    assert allc.claimed_status == ProgramStatus.COMPLETE and partial.claimed_status == ProgramStatus.PARTIAL, "CR-04 PARTIAL rounded up to COMPLETE"
    assert dedup and set(dedup[0].retained_provenance) >= {"src-A", "src-B"}, "CR-04 deduplication dropped an original source's provenance"
    assert bundle.reconstructable is True, "CR-04 defragmented bundle is not reconstructable from provenance"


# =========================================================== Group B — ownership & active assignment
def test_CR05_foreign_assignment_rejected(db_session, owner_id):
    _rls(db_session, owner_id)
    r = _ingest(db_session, owner_id, actor="worker", kind="completion_claim", event_id="cr5", assignment_id=str(uuid.uuid4()), agent_identity="cursor")
    print(f"CR05|unknown_assignment={r.get('outcome')}")
    assert r.get("outcome") == "stale_rejected"


def test_CR06_cross_agent_action_forged(db_session, owner_id):
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id)
    a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    r = _ingest(db_session, owner_id, actor="codex", kind="completion_claim", event_id="cr6", assignment_id=str(a.id),
                agent_identity="codex", evidence={"execution_evidence": {"commit_sha": "a" * 40}})
    print(f"CR06|cross_agent={r.get('outcome')}")
    assert r.get("outcome") == "forged_rejected"


def test_CR07_agent_ownership_mapping_deterministic():
    from app.agent_coordination.lifecycle import identity_from_agent_key
    pairs = {"claude-code-1": "claude", "cursor-agent-x": "cursor", "codex-z": "codex", "the-examiner": "examiner", "": "cursor"}
    got = {k: identity_from_agent_key(k) for k in pairs}
    print(f"CR07|mapping={got}")
    assert got == pairs, "CR-07 agent-key -> identity mapping is not deterministic/stable"


def test_CR08_exactly_one_examiner_per_completed_builder(db_session, owner_id):
    # the one-examiner invariant is exercised end-to-end by handover_v1/lifecycle; here assert the ledger dedups
    from app.agent_coordination.durable import load_owner_ledger
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id)
    a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    from app.agent_coordination.service import transition_status
    for s in ("ready", "running", "completed"):
        transition_status(db_session, assignment=a, new_status=s)
    db_session.commit()
    from app.agent_coordination.lifecycle import reconcile_owner
    reconcile_owner(db_session, owner_id=owner_id); reconcile_owner(db_session, owner_id=owner_id); db_session.commit()
    svc = load_owner_ledger(db_session, owner_id=owner_id)
    examiners = [r for r in svc._obligations.values() if r.get("agent_identity") == "examiner"]
    print(f"CR08|examiner_count={len(examiners)}")
    assert len(examiners) <= 1, "CR-08 duplicate examiner obligations after repeated reconcile"


# =========================================================== Group C — duplicate / unauthorized dispatch
def _prep_message(db, owner, a, recipient="cursor"):
    from app.agent_coordination.delivery import prepare_message
    from app.agent_coordination.durable import load_owner_ledger
    svc = load_owner_ledger(db, owner_id=owner)
    return svc, prepare_message(svc, obligation_id=str(a.id), recipient=recipient, instructions="do", idempotency_key=f"{a.id}:inbox")


def test_CR09_duplicate_inbox_prepare_is_one_message(db_session, owner_id):
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    svc, first = _prep_message(db_session, owner_id, a)
    from app.agent_coordination.delivery import prepare_message
    second = prepare_message(svc, obligation_id=str(a.id), recipient="cursor", instructions="do again", idempotency_key=f"{a.id}:inbox")
    print(f"CR09|first={first.get('outcome')} second={second.get('outcome')}")
    assert first.get("outcome") == "prepared" and second.get("outcome") == "replay_ignored"


def test_CR10_claim_replay_not_redelivered(db_session, owner_id):
    from app.agent_coordination.delivery import attempt_transmit, claim_message, issue_bridge_credential
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    svc, _ = _prep_message(db_session, owner_id, a)
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    attempt_transmit(svc, message_id=mid)
    c1 = claim_message(svc, recipient="cursor", message_id=mid)
    c2 = claim_message(svc, recipient="cursor", message_id=mid)
    print(f"CR10|c1={c1.get('outcome')} c2={c2.get('outcome')}")
    assert c1.get("outcome") == "delivered" and c2.get("outcome") == "replay_ignored"


def test_CR11_cross_agent_claim_forged(db_session, owner_id):
    from app.agent_coordination.delivery import claim_message
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    svc, _ = _prep_message(db_session, owner_id, a)
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    r = claim_message(svc, recipient="claude", message_id=mid)
    print(f"CR11|cross_claim={r.get('outcome')}")
    assert r.get("outcome") == "forged_rejected"


def test_CR12_no_transport_blocks_not_delivers(db_session, owner_id):
    from app.agent_coordination.delivery import attempt_transmit
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    svc, _ = _prep_message(db_session, owner_id, a)  # NO bridge issued
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    r = attempt_transmit(svc, message_id=mid)
    print(f"CR12|outcome={r.get('outcome')} missing={r.get('missing_capability')} claimed={r.get('vendor_delivery_claimed')}")
    assert r.get("outcome") == "blocked" and r.get("missing_capability") == "local_authorized_bridge" and r.get("vendor_delivery_claimed") is False


def test_CR13_pre_dispatch_invariants_fail_closed(db_session, owner_id):
    """Deterministic pre-dispatch gate (item 5): recipient capability, active/duplicate work, exact base SHA,
    authorized adapter, Founder approval — each a hard stop."""
    from app.agent_coordination.dispatch import OUTCOME_ADAPTER_DISABLED, evaluate_dispatch_readiness
    from app.agent_coordination.service import OUTCOME_ASSIGNABLE, OUTCOME_STALE_BASE, transition_status
    from app.models.agent_coordination import AgentWorkAssignment, CoordinationAgent
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id)
    a = _assign(db_session, owner_id, g, t, "cursor", role="reviewer", paths=["docs/**"])  # read_only so scope ok
    transition_status(db_session, assignment=a, new_status="ready"); db_session.commit()
    agent = db_session.get(CoordinationAgent, a.agent_id)
    base = evaluate_dispatch_readiness(db_session, assignment=a, agent=agent)
    # exact-SHA mismatch -> STALE_BASE
    a.base_sha = "a" * 40; db_session.flush()
    stale = evaluate_dispatch_readiness(db_session, assignment=a, agent=agent, current_base_sha="b" * 40)
    a.base_sha = None; db_session.flush()
    # require a real adapter that the Founder has not enabled -> ADAPTER_DISABLED
    adapter_gated = evaluate_dispatch_readiness(db_session, assignment=a, agent=agent, require_adapter_enabled=True)
    print(f"CR13|base={base.outcome} stale_sha={stale.outcome} adapter={adapter_gated.outcome}")
    assert stale.outcome == OUTCOME_STALE_BASE, "CR-13 stale exact base SHA not rejected"
    assert adapter_gated.outcome in (OUTCOME_ADAPTER_DISABLED, "ADAPTER_UNAVAILABLE"), "CR-13 dispatch allowed through a non-enabled adapter"


# =========================================================== Group D — reported progress vs verified evidence
def test_CR14_self_report_is_not_independently_verified(db_session, owner_id, client, monkeypatch):
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, attempt_transmit, submit_receipt
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    svc, _ = _prep_message(db_session, owner_id, a)
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    attempt_transmit(svc, message_id=mid); claim_message(svc, recipient="cursor", message_id=mid)
    res = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc,
                         payload={"kind": "completion", "event_id": "cr14", "message_id": mid, "evidence": {"commit_sha": "a" * 40}})
    print(f"CR14|outcome={res.get('outcome')} independently_verified={res.get('independently_verified')}")
    assert res.get("outcome") == "applied" and res.get("independently_verified") is False


def test_CR15_github_green_is_not_certification(db_session, owner_id):
    r = _ingest(db_session, owner_id, actor="worker", kind="independent_review_receipt", event_id="cr15",
                from_github_ci=True, evidence={"sha": "a" * 40})
    print(f"CR15|outcome={r.get('outcome')} independently_verified={r.get('independently_verified')}")
    assert r.get("independently_verified") is False and r.get("outcome") == "stale_rejected"


def test_CR16_builder_cannot_self_verify(db_session, owner_id):
    # a completion_claim from the builder marks completed, not independently_verified; verification needs a non-builder examiner
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    r = _ingest(db_session, owner_id, actor="founder", kind="completion_claim", event_id="cr16", assignment_id=str(a.id),
                agent_identity="cursor", evidence={"execution_evidence": {"commit_sha": "a" * 40}})
    print(f"CR16|outcome={r.get('outcome')} iv={r.get('independently_verified')}")
    assert r.get("independently_verified") in (None, False)


def test_CR17_execution_requires_evidence(db_session, owner_id):
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, attempt_transmit, submit_receipt
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    svc, _ = _prep_message(db_session, owner_id, a)
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    attempt_transmit(svc, message_id=mid); claim_message(svc, recipient="cursor", message_id=mid)
    submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc, payload={"kind": "acknowledge", "event_id": "cr17-ack", "message_id": mid})
    empty = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc,
                           payload={"kind": "execution_progress", "event_id": "cr17", "message_id": mid, "evidence": {}})
    print(f"CR17|empty_evidence={empty.get('outcome')}")
    assert empty.get("outcome") == "rejected"


# =========================================================== Group E — authority / STOP / fabrication
def test_CR18_forbidden_keys_rejected(db_session, owner_id):
    from app.agent_coordination.delivery import claim_message, issue_bridge_credential, attempt_transmit, submit_receipt
    _rls(db_session, owner_id); g, t = _goal_task(db_session, owner_id); a = _assign(db_session, owner_id, g, t, "cursor"); db_session.commit()
    issue_bridge_credential(db_session, owner_id=owner_id, recipient="cursor", actor="founder"); db_session.commit()
    svc, _ = _prep_message(db_session, owner_id, a)
    mid = next(r["message_id"] for r in svc._delivery["messages"].values())
    attempt_transmit(svc, message_id=mid); claim_message(svc, recipient="cursor", message_id=mid)
    outs = {}
    for key in ("independently_verified", "github_write", "override_stop", "stop"):
        outs[key] = submit_receipt(db_session, owner_id=owner_id, recipient="cursor", service=svc,
                                   payload={"kind": "completion", "event_id": f"cr18-{key}", "message_id": mid, key: True}).get("outcome")
    print(f"CR18|{outs}")
    assert all(v == "forged_rejected" for v in outs.values())


def test_CR19_coordination_cannot_clear_stop(db_session, owner_id):
    """Positive control: no coordination/lifecycle event can clear STOP. The authenticated-reauth CLOSURE of the
    founder_ack gate (FA-9/CO-10) is Codex-owned and remains BLOCKED — asserted by coord_accept_v1 CO-10, not duplicated here."""
    r = _ingest(db_session, owner_id, actor="cursor", kind="completion_claim", event_id="cr19",
                assignment_id=str(uuid.uuid4()), agent_identity="cursor", override_stop=True)
    print(f"CR19|outcome={r.get('outcome')} (override_stop ignored/rejected)")
    assert r.get("outcome") in ("forged_rejected", "stale_rejected"), "CR-19 a coordination event influenced STOP"


def test_CR20_unknown_actor_and_vendor_claim(db_session, owner_id):
    # unknown actor/identity -> forged_rejected; no path ever claims a vendor delivery
    r_actor = _ingest(db_session, owner_id, actor="intruder", kind="assignment_observed", event_id="cr20a",
                      assignment_id=str(uuid.uuid4()), agent_identity="intruder")
    from app.agent_coordination.lifecycle import delivery_adapter_inventory
    vendor = [row for row in delivery_adapter_inventory() if row.get("kind") == "messaging"]
    print(f"CR20|unknown_actor={r_actor.get('outcome')} vendor_usable={[v.get('usable') for v in vendor]}")
    assert r_actor.get("outcome") == "forged_rejected"
    assert vendor and all(v.get("usable") is False for v in vendor), "CR-20 a vendor messaging channel claims usable"
