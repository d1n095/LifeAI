"""#312 provider security corrections — independent closure verification (examiner-owned).

Subject: #312 22458257 (based on #309 aff5de1a). Verifies that PI-STOP, ICV-WIRING, PI-DEDUP and
RST-4 are ACTUALLY closed, driving the real corrected functions. Each prior finding's required
behaviour (previously a strict-xfail in pistop_v1 / indepcomp_v1 / composed_v1 / spend_authority_v1)
is now asserted as a plain PASS.

#312 changed the ingest signature: ingest_authenticated_provider_event now takes `db` and fails
closed (authority_unestablished) without a durable session. These tests supply the real db.

No live/paid calls (egress guard). first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "whsec_examiner_fc312"
UA = "Cursor-Agent-Webhook/1.0"


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _body(status="FINISHED", agent_id="ext_312", ts=None) -> bytes:
    p = {"event": "statusChange", "status": status, "id": agent_id}
    if ts:
        p["timestamp"] = ts
    return json.dumps(p).encode()


def _svc_bound(owner, recipient="cursor", agent_id="ext_312"):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc = DurableCoordinationService()
    aid = "asg_" + uuid.uuid4().hex[:8]
    svc.assign(agent_identity=recipient, workstream="build", owner_id=owner,
               instruction_id="i1", assignment_id=aid, actor="founder")
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor",
                             external_agent_id=agent_id, recipient=recipient)
    return svc, aid


def _ingest(svc, owner, *, db, status="FINISHED", agent_id="ext_312", ts=None):
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    body = _body(status, agent_id=agent_id, ts=ts)
    return ingest_authenticated_provider_event(svc, owner_id=owner, secret=SECRET, raw_body=body,
                                               signature=_sig(body), user_agent=UA, fixture=True, db=db)


# ---------------------------------------------------------------------------
# PI-STOP — ingestion now consults the kill switch at the transition boundary
# ---------------------------------------------------------------------------

def test_FC312_PISTOP_clean_completes(db_session, owner_id):
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    r = _ingest(svc, owner, db=db_session, status="FINISHED")
    assert r.get("transition") == "completed", r


def test_FC312_PISTOP_stop_blocks_completion(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop")
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)   # positive control
    r = _ingest(svc, owner, db=db_session, status="FINISHED")
    assert r.get("transition") != "completed", f"completed under STOP: {r}"
    assert r.get("outcome") == "stop_rejected" and r.get("stop_enforced") is True, r


def test_FC312_PISTOP_no_session_fails_closed(db_session, owner_id):
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    r = _ingest(svc, owner, db=None, status="FINISHED")   # no durable session
    assert r.get("transition") != "completed"
    assert r.get("outcome") == "authority_unestablished", r


# ---------------------------------------------------------------------------
# PI-DEDUP — timestamp-less duplicate now deduped via stable (raw_body) event_id
# ---------------------------------------------------------------------------

def test_FC312_PIDEDUP_timestampless_duplicate_is_ignored(db_session, owner_id):
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    r1 = _ingest(svc, owner, db=db_session, status="RUNNING", ts=None)
    r2 = _ingest(svc, owner, db=db_session, status="RUNNING", ts=None)
    assert r2.get("outcome") == "replay_ignored", f"timestampless duplicate not deduped: {r2}"


# ---------------------------------------------------------------------------
# ICV-WIRING — provider completion is gated on an independent registry attestation
# ---------------------------------------------------------------------------

def test_FC312_ICV_requires_attestation_then_allows(db_session, superuser_db, owner_id):
    from app.agent_coordination.provider_ingestion import continue_after_independent_verification
    from app.mainai_verification_registry.service import record_verification_attestation
    owner = str(owner_id)
    svc, aid = _svc_bound(owner)
    assert _ingest(svc, owner, db=db_session, status="FINISHED").get("transition") == "completed"
    builder = "cursor:ext_312"
    sha = "a" * 40
    component = f"provider_assignment:{aid}"
    # No attestation yet -> continuation must be refused as verification_required.
    gated = continue_after_independent_verification(db_session, svc, owner_id=owner, assignment_id=aid,
                                                    builder_identity=builder, candidate_sha=sha, component_id=component)
    assert gated.get("outcome") == "verification_required" and gated.get("independently_verified") is False, gated
    # Record an independent (non-builder) PASS via the privileged writer.
    record_verification_attestation(superuser_db, component_id=component, candidate_sha=sha,
                                    builder_identity=builder, examiner_identity="examiner:independent_1",
                                    review_result="PASS", owner_id=owner_id, evidence_summary="independent rerun green")
    superuser_db.commit()
    allowed = continue_after_independent_verification(db_session, svc, owner_id=owner, assignment_id=aid,
                                                      builder_identity=builder, candidate_sha=sha, component_id=component)
    assert allowed.get("outcome") in {"applied", "allowed", "verified", "attested"}, allowed
    assert allowed.get("independently_verified") is True, allowed


# ---------------------------------------------------------------------------
# RST-4 — bare prefixed credential embedded in free text is now redacted
# ---------------------------------------------------------------------------

def test_FC312_RST4_bare_prefixed_secret_redacted(db_session):
    from app.mainai_restrictions.service import redact_secrets
    bare = "sk-live-" + "A1b2C3d4E5" * 4
    out = json.dumps(redact_secrets({"note": f"the value is {bare} please use it"}))
    assert bare not in out, "bare prefixed credential not redacted (RST-4 not closed)"


def test_FC312_RST4_residual_nonprefixed_highentropy(db_session):
    """HONEST residual: the fix is prefix-based (sk-/ghp_/xoxb-/...). A bare high-entropy value
    with NO known prefix and NO keyword is still not redacted. Documented P4 observation, not a
    reopened blocker — recorded so the closure claim is not overstated."""
    from app.mainai_restrictions.service import redact_secrets
    opaque = "Zx9Qw" + "7Kp2Lm4Rt" * 4  # 45 chars, no known prefix, no 'token='/'secret=' keyword
    out = json.dumps(redact_secrets({"note": f"opaque {opaque} here"}))
    # We assert CURRENT behaviour (still present) so this test stays truthful; if a future entropy
    # detector redacts it, flip this to assert redaction.
    assert opaque in out, "unexpected: non-prefixed high-entropy value was redacted (fix improved)"
