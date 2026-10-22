"""Operational Handover certification (HO-*) for durable agent coordination (#298 agent_coordination/durable.py).
Tests the REAL DurableCoordinationService against the 10 operational scenarios. Credential-free, no paid calls.
A capability genuinely absent / owned elsewhere is recorded BLOCKED, never PASS. Signatures match durable.py exactly.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

svc_mod = pytest.importorskip("app.agent_coordination.durable")


def S():
    return svc_mod.DurableCoordinationService()


def _assign(s, agent, workstream, instr, owner="o1"):
    return s.assign(agent_identity=agent, workstream=workstream, owner_id=owner, instruction_id=instr)


# ---- 1: three agents, distinct ownership / identity / status / chronology
def test_HO1_three_agents_distinct_obligations():
    s = S()
    a = _assign(s, "cursor", "ws-a", "i1"); b = _assign(s, "codex", "ws-b", "i2"); c = _assign(s, "claude", "ws-c", "i3")
    assert a.accepted and b.accepted and c.accepted, (a.as_dict(), b.as_dict(), c.as_dict())
    assert len({a.assignment_id, b.assignment_id, c.assignment_id}) == 3
    view = s.founder_view()
    owners = {o["agent_identity"] for o in view["obligations"]}
    print(f"HO1|agents={owners} obligations={len(view['obligations'])}")
    assert owners == {"cursor", "codex", "claude"}


# ---- 2: stale completion / queue-empty after a newer assignment must not lose current work
def test_HO2_stale_queue_empty_rejected_when_active():
    s = S()
    _assign(s, "cursor", "ws", "i1")
    r = s.claim_queue_empty(reporter="cursor")
    print(f"HO2|queue_empty_accepted={r.accepted} reason={r.as_dict().get('reason')}")
    assert not r.accepted, "HO-2 queue-empty accepted while an active obligation exists"


def test_HO2b_superseded_old_completion_cannot_revive():
    s = S()
    a = _assign(s, "cursor", "ws", "i1")
    sup = s.supersede(assignment_id=a.assignment_id, actor="founder", successor_agent="cursor", instruction_id="i2")
    assert sup.accepted, sup.as_dict()
    late = s.complete(assignment_id=a.assignment_id, agent_identity="cursor", evidence={"generation": 0, "commit_sha": "a" * 40})
    print(f"HO2b|late_completion_accepted={late.accepted} outcome={late.as_dict().get('outcome')}")
    assert not late.accepted or late.as_dict().get("outcome") != "applied", "HO-2b stale completion mutated a superseded obligation"


# ---- 3: only agents whose latest work ended are eligible for another assignment (same canonical work while active)
def test_HO3_active_obligation_blocks_duplicate_assignment():
    s = S()
    first = _assign(s, "cursor", "ws", "i1")
    dup = _assign(s, "cursor", "ws", "i1")
    print(f"HO3|first={first.accepted} dup_accepted={dup.accepted} dup_outcome={dup.as_dict().get('outcome')} same_id={dup.assignment_id == first.assignment_id}")
    assert dup.as_dict().get("outcome") in {"duplicate", "idempotent", "already_active", "rejected"} or dup.assignment_id == first.assignment_id, \
        "HO-3 a second independent obligation was minted for the same active work"


# ---- 4: duplicate reports / replayed instructions are idempotent, never a second assignment
def test_HO4_replayed_event_is_idempotent():
    s = S()
    a = _assign(s, "cursor", "ws", "i1")
    eid = str(uuid.uuid4())
    s.acknowledge(assignment_id=a.assignment_id, agent_identity="cursor", event_id=eid)
    r2 = s.acknowledge(assignment_id=a.assignment_id, agent_identity="cursor", event_id=eid)
    print(f"HO4|replay_outcome={r2.as_dict().get('outcome')} accepted={r2.accepted}")
    assert r2.as_dict().get("outcome") in {"replay_ignored", "idempotent"} or not r2.accepted, "HO-4 replayed event re-applied"


# ---- 5: repeated planning is detected as stalled without generating more obligations
def test_HO5_repeated_planning_detected_as_stalled():
    s = S()
    a = _assign(s, "cursor", "ws", "i1")
    for _ in range(5):
        s.record_planning(assignment_id=a.assignment_id, agent_identity="cursor")
    rec = s.get(a.assignment_id)
    stalled = rec.get("stalled") is True or any("stall" in str(e.get("kind", "")).lower() for e in rec.get("events", []))
    print(f"HO5|obligations={len(s.founder_view()['obligations'])} stalled={stalled}")
    assert len(s.founder_view()["obligations"]) == 1, "HO-5 planning created extra obligations"
    assert stalled, "HO-5 repeated planning without execution is not flagged stalled"


# ---- 6: 'completed' needs execution evidence; planning / instruction alone is not execution
def test_HO6_planning_is_not_execution():
    s = S()
    a = _assign(s, "cursor", "ws", "i1")
    bad = s.record_execution_progress(assignment_id=a.assignment_id, agent_identity="cursor",
                                      evidence_kind="plan", evidence={"note": "I will do it"})
    print(f"HO6|plan_as_execution_accepted={bad.accepted} reason={bad.as_dict().get('outcome')}")
    assert not bad.accepted, "HO-6 planning evidence accepted as execution progress"


# ---- 7: a completed candidate -> independent-review obligation; builder cannot self-verify
def test_HO7_independent_review_cannot_be_builder():
    s = S()
    a = _assign(s, "cursor", "ws", "i1")
    s.acknowledge(assignment_id=a.assignment_id, agent_identity="cursor")
    s.record_execution_progress(assignment_id=a.assignment_id, agent_identity="cursor", evidence_kind="diff", evidence={"commit_sha": "a" * 40})
    s.complete(assignment_id=a.assignment_id, agent_identity="cursor", evidence={"commit_sha": "a" * 40, "tests": "pass"})
    self_v = s.independently_verify(assignment_id=a.assignment_id, examiner_identity="cursor", evidence={})
    indep = s.independently_verify(assignment_id=a.assignment_id, examiner_identity="claude", evidence={"candidate_sha": "a" * 40, "review": "PASS"})
    print(f"HO7|builder_self_verify={self_v.accepted} independent={indep.accepted}")
    assert not self_v.accepted, "HO-7 builder self-verified its own work"
    assert indep.accepted, "HO-7 a legitimate independent examiner could not verify a completed obligation"


# ---- 8: artifact handoff — checksum verified; unavailable private/vendor channel truthfully BLOCKED
def test_HO8_checksum_and_blocked_channels():
    s = S()
    payload = b"hello-bundle"
    digest = svc_mod.artifact_sha256(payload)
    art = s.register_artifact(producer_identity="codex", consumer_identity="claude", sha256=digest,
                              byte_length=len(payload), provenance={"src": "codex"})
    assert art.accepted, art.as_dict()
    hid = (art.detail or {}).get("handoff_id")
    # available transport with a matching payload digest -> delivered
    ok = s.transfer_artifact(handoff_id=hid, actor="codex", transport="in_process_registry")
    rec = s.receive_artifact(handoff_id=hid, consumer_identity="claude", payload=payload)
    # tampered payload -> rejected
    bad = s.receive_artifact(handoff_id=hid, consumer_identity="claude", payload=b"tampered")
    # private/vendor channels -> BLOCKED with named missing capability (not faked as delivered)
    blocked = {t: s.transfer_artifact(handoff_id=hid, actor="codex", transport=t)
               for t in ("vendor_private_workspace", "github_publication", "personal_credentials")}
    print(f"HO8|transfer_ok={ok.accepted} receive_ok={rec.accepted} tampered_rejected={not bad.accepted} "
          f"blocked={{t: v.as_dict().get('outcome') for t, v in blocked.items()}}")
    assert ok.accepted, "HO-8 in-process authorized transfer failed"
    assert not bad.accepted, "HO-8 a tampered artifact passed checksum verification"
    for t, v in blocked.items():
        assert not v.accepted, f"HO-8 unavailable channel {t} was accepted"
        assert "blocked" in str(v.as_dict().get("outcome", "")).lower() or v.as_dict().get("missing_capability"), \
            f"HO-8 {t} not truthfully BLOCKED: {v.as_dict()}"


# ---- 9: consolidated Founder view; coordination work is not escalated to the Founder
def test_HO9_founder_view_consolidated():
    s = S()
    _assign(s, "cursor", "ws-a", "i1"); _assign(s, "codex", "ws-b", "i2")
    view = s.founder_view()
    print(f"HO9|keys={sorted(view)[:8]} obligations={len(view.get('obligations', []))}")
    assert "obligations" in view and len(view["obligations"]) == 2


# ---- 10: coordination cannot forge approvals / override STOP / alter kernel / impersonate
def test_HO10_coordination_cannot_escalate_privilege():
    s = S()
    for grant in ("founder_approval", "clear_stop", "github_write", "security_kernel_authority"):
        r = s.escalate_privilege(actor="cursor", grant=grant)
        assert not r.accepted, f"HO-10 coordination granted {grant}"
    print("HO10|all privilege-escalation attempts rejected (owner=codex_founder_authority)")


def test_HO10b_cross_agent_impersonation_rejected():
    s = S()
    a = _assign(s, "cursor", "ws", "i1")
    s.acknowledge(assignment_id=a.assignment_id, agent_identity="cursor")
    imp = s.complete(assignment_id=a.assignment_id, agent_identity="codex", evidence={"commit_sha": "a" * 40})
    print(f"HO10b|impersonated_completion_accepted={imp.accepted} outcome={imp.as_dict().get('outcome')}")
    assert not imp.accepted, "HO-10b one agent completed another agent's obligation"
