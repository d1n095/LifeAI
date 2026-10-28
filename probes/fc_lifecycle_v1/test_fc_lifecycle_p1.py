"""Priority 1 — Authenticated provider lifecycle (end-to-end acceptance).

Lifecycle under test (real production functions, NOT #314's fixture=True shortcut):
  authorized assignment -> provider binding -> AUTHENTICATED-LOCAL signed event ->
  durable ledger transition -> persist/reload -> independent-verification-gated continuation.

Provider events are locally generated and correctly HMAC-signed with a caller-held secret.
That is AUTHENTICATED-LOCAL evidence (we hold the secret), explicitly NOT a genuine external
Cursor delivery (no api.cursor.com call). Each test maps to one of the 8 required properties.

Target: #314 head 2308b697 (composed #312->#314 provider lineage). DB-backed (tests.conftest).
Loopback-only (egress guard); no paid calls; no production mutation. first_cycle_go=false.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from _guards import _no_egress  # noqa: F401
from _harness import (  # noqa: F401
    UA,
    _privileges,
    assign,
    bind,
    event_body,
    ingest,
    new_service,
    owner_id,
    provider_events,
    sig,
)

AGENT = "ext-agent-1"


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _bound_assignment(service, owner, *, agent=AGENT, recipient="cursor", ws="ws-main", instr="instr-main"):
    aid = assign(service, owner=owner, agent=recipient, workstream=ws, instruction=instr)
    b = bind(service, owner=owner, assignment_id=aid, agent_id=agent, recipient=recipient)
    assert b["outcome"] == "bound", b
    return aid


# ---------------------------------------------------------------------------
# REQ 1 + 2 — provider identity must match the authorized assignment+owner; forged/cross-owner
# ---------------------------------------------------------------------------
def test_PL1_identity_binding_and_forgery(db_session, owner_id):
    svc = new_service()
    aid = assign(svc, owner=owner_id, agent="cursor", workstream="ws1", instruction="i1")

    # cross-owner binding rejected; recipient mismatch rejected
    import uuid as _uuid
    assert bind(svc, owner=_uuid.uuid4(), assignment_id=aid, agent_id=AGENT, recipient="cursor")["outcome"] == "forged_rejected"
    assert bind(svc, owner=owner_id, assignment_id=aid, agent_id=AGENT, recipient="claude")["outcome"] == "rejected"

    assert bind(svc, owner=owner_id, assignment_id=aid, agent_id=AGENT, recipient="cursor")["outcome"] == "bound"

    body = event_body(status="FINISHED", agent_id=AGENT)
    # forged signature -> forged_rejected, NO transition
    forged = ingest(svc, owner=owner_id, body=body, signature="sha256=deadbeef", db=db_session)
    assert forged["outcome"] == "forged_rejected"
    assert svc.get(aid)["status"] == "assigned"

    # unbound external agent -> rejected
    other = event_body(status="FINISHED", agent_id="unbound-agent")
    assert ingest(svc, owner=owner_id, body=other, db=db_session)["outcome"] == "rejected"

    # correctly-signed but CROSS-OWNER event (bound to owner_id, ingested as a different owner)
    cross = ingest(svc, owner=_uuid.uuid4(), body=body, db=db_session)
    assert cross["outcome"] == "forged_rejected"
    assert svc.get(aid)["status"] == "assigned"   # still no transition


# ---------------------------------------------------------------------------
# REQ 2 — replayed and reordered authenticated events cannot advance execution
# ---------------------------------------------------------------------------
def test_PL2_replay_and_reorder_rejected(db_session, owner_id):
    svc = new_service()
    aid = _bound_assignment(svc, owner_id)

    t2 = _iso(datetime.now(timezone.utc))
    body2 = event_body(status="FINISHED", agent_id=AGENT, timestamp=t2)
    r = ingest(svc, owner=owner_id, body=body2, db=db_session)
    assert r["outcome"] == "applied" and r["transition"] == "completed"
    assert len(provider_events(svc, aid, transition="completed")) == 1

    # exact replay -> replay_ignored, still exactly one completion
    again = ingest(svc, owner=owner_id, body=body2, db=db_session)
    assert again["outcome"] == "replay_ignored"
    assert len(provider_events(svc, aid, transition="completed")) == 1

    # an OLDER-timestamp event arriving after a newer accepted one -> reordered_rejected
    t1 = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    older = event_body(status="RUNNING", agent_id=AGENT, timestamp=t1)
    assert ingest(svc, owner=owner_id, body=older, db=db_session)["outcome"] == "reordered_rejected"


# ---------------------------------------------------------------------------
# REQ 3 — STOP/revocation during execution prevents unauthorized transitions
# ---------------------------------------------------------------------------
def test_PL3_stop_during_execution_blocks_completion(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch

    svc = new_service()
    aid = _bound_assignment(svc, owner_id)
    svc.acknowledge(assignment_id=aid, agent_identity="cursor")   # mid-execution

    activate_kill_switch(db_session, owner_id=owner_id, reason="founder STOP during execution")

    body = event_body(status="FINISHED", agent_id=AGENT, timestamp=_iso(datetime.now(timezone.utc)))
    r = ingest(svc, owner=owner_id, body=body, db=db_session)
    assert r["outcome"] == "stop_rejected" and r.get("stop_enforced") is True
    assert svc.get(aid)["status"] != "completed"       # STOP fenced the transition before it applied


# ---------------------------------------------------------------------------
# REQ 4 — crash/restart cannot create duplicate completion or spending effects
# ---------------------------------------------------------------------------
def test_PL4_restart_no_duplicate_completion(db_session, owner_id):
    from app.agent_coordination.persist import persist_owner_ledger
    from app.agent_coordination.durable import load_owner_ledger

    svc = new_service()
    aid = _bound_assignment(svc, owner_id)
    body = event_body(status="FINISHED", agent_id=AGENT, timestamp=_iso(datetime.now(timezone.utc)))
    assert ingest(svc, owner=owner_id, body=body, db=db_session)["transition"] == "completed"

    # durable persist, then a FRESH process rehydrates purely from the ledger
    persist_owner_ledger(db_session, owner_id=owner_id, service=svc)
    svc2 = load_owner_ledger(db_session, owner_id=owner_id)
    assert svc2.get(aid) is not None and svc2.get(aid)["status"] == "completed"
    assert len(provider_events(svc2, aid, transition="completed")) == 1   # restored, exactly one

    # the SAME authenticated event replayed after restart is ignored (no duplicate completion)
    replay = ingest(svc2, owner=owner_id, body=body, db=db_session)
    assert replay["outcome"] == "replay_ignored"
    assert len(provider_events(svc2, aid, transition="completed")) == 1
    assert svc2.get(aid)["status"] == "completed"


# ---------------------------------------------------------------------------
# REQ 5 — provider-reported FINISHED never becomes independent certification
# ---------------------------------------------------------------------------
def test_PL5_finished_is_not_certification(db_session, owner_id):
    svc = new_service()
    aid = _bound_assignment(svc, owner_id)
    body = event_body(status="FINISHED", agent_id=AGENT, timestamp=_iso(datetime.now(timezone.utc)))
    r = ingest(svc, owner=owner_id, body=body, db=db_session)

    assert r["transition"] == "completed"
    assert r["independently_verified"] is False and r["verification_required"] is True
    assert r["provider_reported_completion"] is True

    rec = svc.get(aid)
    assert rec["status"] == "completed" and rec["status"] != "independently_verified"
    view = svc.founder_view()
    assert any(o["assignment_id"] == aid for o in view["finished"])
    assert all(o["assignment_id"] != aid for o in view["independently_certified"])
    assert view["provider_ingestion"]["independently_verified"] is False
    assert view["fabricated_progress"] is False


# ---------------------------------------------------------------------------
# REQ 6 — independent verification is required before verification-gated continuation
# ---------------------------------------------------------------------------
def test_PL6_continuation_requires_independent_attestation(db_session, superuser_db, owner_id):
    from sqlalchemy.exc import InternalError, ProgrammingError

    from app.agent_coordination.provider_ingestion import continue_after_independent_verification
    from app.mainai_verification_registry import record_verification_attestation
    from app.mainai_verification_registry.service import VerificationRegistryError

    svc = new_service()
    aid = _bound_assignment(svc, owner_id)
    body = event_body(status="FINISHED", agent_id=AGENT, timestamp=_iso(datetime.now(timezone.utc)))
    ingest(svc, owner=owner_id, body=body, db=db_session)

    sha = "a" * 40
    component = f"provider_assignment:{aid}"
    builder = "cursor"

    # no attestation yet -> continuation is refused
    r0 = continue_after_independent_verification(
        db_session, svc, owner_id=str(owner_id), assignment_id=aid,
        builder_identity=builder, candidate_sha=sha, component_id=component)
    assert r0["outcome"] == "verification_required" and r0["independently_verified"] is False

    # SECURITY: the ordinary MainAI runtime role is DB-DENIED from writing verification records,
    # so a compromised provider-runtime cannot forge its own independent attestation.
    with pytest.raises((InternalError, ProgrammingError)):
        record_verification_attestation(
            db_session, component_id=component, candidate_sha=sha,
            builder_identity="cursor", examiner_identity="examiner", review_result="PASS",
            owner_id=owner_id, evidence_summary="runtime forge attempt")
    db_session.rollback()

    # a BUILDER self-attestation cannot be recorded at all (BUILDER_CANNOT_SELF_VERIFY),
    # even by the privileged writer (independence is enforced before any insert)
    with pytest.raises(VerificationRegistryError):
        record_verification_attestation(
            superuser_db, component_id=component, candidate_sha=sha,
            builder_identity=builder, examiner_identity=builder, review_result="PASS",
            owner_id=owner_id, evidence_summary="self")

    # a PASS for a DIFFERENT sha (written by the privileged authority) does not satisfy this candidate
    record_verification_attestation(
        superuser_db, component_id=component, candidate_sha="b" * 40,
        builder_identity=builder, examiner_identity="examiner", review_result="PASS",
        owner_id=owner_id, evidence_summary="different sha")
    superuser_db.commit()
    r1 = continue_after_independent_verification(
        db_session, svc, owner_id=str(owner_id), assignment_id=aid,
        builder_identity=builder, candidate_sha=sha, component_id=component)
    assert r1["outcome"] == "verification_required"

    # a genuine INDEPENDENT PASS (examiner != builder) for the exact sha -> continuation attested
    record_verification_attestation(
        superuser_db, component_id=component, candidate_sha=sha,
        builder_identity=builder, examiner_identity="examiner", review_result="PASS",
        owner_id=owner_id, evidence_summary="independent pass")
    superuser_db.commit()
    r2 = continue_after_independent_verification(
        db_session, svc, owner_id=str(owner_id), assignment_id=aid,
        builder_identity=builder, candidate_sha=sha, component_id=component)
    assert r2["outcome"] == "attested" and r2["independently_verified"] is True


# ---------------------------------------------------------------------------
# REQ 7 — one blocked agent cannot prevent unrelated authorized work
# ---------------------------------------------------------------------------
def test_PL7_blocked_agent_does_not_block_unrelated_work(db_session, owner_id):
    svc = new_service()
    # agent A -> BLOCKED via an authenticated non-completion event
    a_agent = "ext-A"
    aid_a = assign(svc, owner=owner_id, agent="cursor", workstream="wsA", instruction="iA")
    bind(svc, owner=owner_id, assignment_id=aid_a, agent_id=a_agent, recipient="cursor")
    blk = ingest(svc, owner=owner_id, body=event_body(status="BLOCKED", agent_id=a_agent,
                 timestamp=_iso(datetime.now(timezone.utc))), db=db_session)
    assert svc.get(aid_a)["status"] == "blocked", blk

    # agent B (unrelated workstream) still advances to completed
    b_agent = "ext-B"
    aid_b = assign(svc, owner=owner_id, agent="claude", workstream="wsB", instruction="iB")
    bind(svc, owner=owner_id, assignment_id=aid_b, agent_id=b_agent, recipient="claude")
    r = ingest(svc, owner=owner_id, body=event_body(status="FINISHED", agent_id=b_agent,
               timestamp=_iso(datetime.now(timezone.utc))), db=db_session)
    assert r["transition"] == "completed" and svc.get(aid_b)["status"] == "completed"

    view = svc.founder_view()
    assert any(o["assignment_id"] == aid_a for o in view["blocked"])
    assert any(o["assignment_id"] == aid_b for o in view["finished"])


# ---------------------------------------------------------------------------
# REQ 8 — Founder-visible status reflects actual PERSISTED evidence, not fabricated progress
# ---------------------------------------------------------------------------
def test_PL8_founder_view_reflects_persisted_ledger(db_session, owner_id):
    from app.agent_coordination.persist import persist_owner_ledger
    from app.agent_coordination.durable import load_owner_ledger

    svc = new_service()
    aid_done = assign(svc, owner=owner_id, agent="cursor", workstream="wsDone", instruction="iDone")
    bind(svc, owner=owner_id, assignment_id=aid_done, agent_id="ext-done", recipient="cursor")
    ingest(svc, owner=owner_id, body=event_body(status="FINISHED", agent_id="ext-done",
           timestamp=_iso(datetime.now(timezone.utc))), db=db_session)

    aid_blk = assign(svc, owner=owner_id, agent="claude", workstream="wsBlk", instruction="iBlk")
    bind(svc, owner=owner_id, assignment_id=aid_blk, agent_id="ext-blk", recipient="claude")
    ingest(svc, owner=owner_id, body=event_body(status="BLOCKED", agent_id="ext-blk",
           timestamp=_iso(datetime.now(timezone.utc))), db=db_session)

    persist_owner_ledger(db_session, owner_id=owner_id, service=svc)

    # the Founder view built from the RELOADED ledger == the persisted truth, nothing fabricated
    reloaded = load_owner_ledger(db_session, owner_id=owner_id)
    view = reloaded.founder_view()
    assert view["fabricated_progress"] is False and view["invented_vendor_access"] is False
    assert reloaded.get(aid_done)["status"] == "completed"
    assert any(o["assignment_id"] == aid_done for o in view["finished"])
    assert all(o["assignment_id"] != aid_done for o in view["independently_certified"])  # provider-only != certified
    assert any(o["assignment_id"] == aid_blk for o in view["blocked"])
