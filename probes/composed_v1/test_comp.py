"""#309 composed integration — adversarial acceptance of provider_ingestion + mainai_restrictions (CMP-*/RST-*).

Drives the REAL #309 functions on aff5de1a: provider_ingestion.bind_provider_assignment /
ingest_authenticated_provider_event (in-memory DurableCoordinationService + the official HMAC), and
mainai_restrictions.encounter_restriction / lookup_restriction_lessons / redact_secrets. Reports which previously
NOT_IMPLEMENTED requirements genuinely flip to PASS, and any residual gap. No live/paid calls, no network, isolated DB.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "composed-webhook-secret-0123456789abcdef"


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _svc_with_obligation(owner="11111111-1111-1111-1111-111111111111", recipient="cursor", aid=None):
    from app.agent_coordination.durable import DurableCoordinationService
    svc = DurableCoordinationService()
    aid = aid or str(uuid.uuid4())
    svc.assign(agent_identity=recipient, workstream="build", owner_id=owner, instruction_id="i1", assignment_id=aid, actor="founder")
    return svc, owner, aid


def _body(status="FINISHED", agent_id="ext_ag_1", ts=None):
    p = {"event": "statusChange", "status": status, "id": agent_id}
    if ts:
        p["timestamp"] = ts
    return json.dumps(p).encode()


def _ingest(svc, owner, body, *, fixture=True, now=None):
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    return ingest_authenticated_provider_event(svc, owner_id=owner, secret=SECRET, raw_body=body,
                                               signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0",
                                               fixture=fixture, now=now)


# ===================== Priority 3 — provider ingestion (flips PEVI) =====================
def test_CMP1_bind_rejects_cross_owner_and_recipient_mismatch(db_session, owner_id):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation(recipient="cursor")
    cross = bind_provider_assignment(svc, owner_id="99999999-9999-9999-9999-999999999999", assignment_id=aid, provider="cursor", external_agent_id="ext1", recipient="cursor")
    mism = bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext1", recipient="claude")
    ok = bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext1", recipient="cursor")
    print(f"CMP1|cross_owner={cross.get('outcome')} recipient_mismatch={mism.get('outcome')} ok={ok.get('outcome')}")
    assert cross.get("outcome") == "forged_rejected" and mism.get("outcome") == "rejected" and ok.get("outcome") == "bound"


def test_CMP2_forged_hmac_and_unbound_rejected(db_session, owner_id):
    svc, owner, aid = _svc_with_obligation()
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    body = _body()
    forged = ingest_authenticated_provider_event(svc, owner_id=owner, secret=SECRET, raw_body=body, signature="sha256=00", user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    unbound = _ingest(svc, owner, body)  # no binding yet
    print(f"CMP2|forged_hmac={forged.get('outcome')} unbound={unbound.get('outcome')}")
    assert forged.get("outcome") == "forged_rejected" and unbound.get("outcome") == "rejected"


def test_CMP3_replay_and_reorder_rejected(db_session, owner_id):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_r", recipient="cursor")
    now = datetime.now(timezone.utc)
    t2 = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    t1 = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    first = _ingest(svc, owner, _body(status="RUNNING", agent_id="ext_r", ts=t2), now=now)
    replay = _ingest(svc, owner, _body(status="RUNNING", agent_id="ext_r", ts=t2), now=now)
    older = _ingest(svc, owner, _body(status="FINISHED", agent_id="ext_r", ts=t1), now=now)
    print(f"CMP3|first={first.get('outcome')} replay={replay.get('outcome')} reordered={older.get('outcome')}")
    assert replay.get("outcome") == "replay_ignored" and older.get("outcome") == "reordered_rejected"


def test_CMP4_expired_or_implausible_rejected(db_session, owner_id):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_e", recipient="cursor")
    now = datetime.now(timezone.utc)
    stale = _ingest(svc, owner, _body(agent_id="ext_e", ts=(now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")), now=now)
    future = _ingest(svc, owner, _body(agent_id="ext_e", ts=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")), now=now)
    print(f"CMP4|stale={stale.get('outcome')} future={future.get('outcome')}")
    assert stale.get("outcome") == "expired_rejected" and future.get("outcome") == "expired_rejected"


def test_CMP5_cross_owner_event_rejected(db_session, owner_id):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_x", recipient="cursor")
    other = _ingest(svc, "22222222-2222-2222-2222-222222222222", _body(agent_id="ext_x"))
    print(f"CMP5|cross_owner_event={other.get('outcome')}")
    assert other.get("outcome") == "forged_rejected"


def test_CMP6_finished_completes_and_prepares_examiner_not_verified(db_session, owner_id):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_f", recipient="cursor")
    r = _ingest(svc, owner, _body(status="FINISHED", agent_id="ext_f"))
    obl = svc.get(aid)
    examiners = [o for o in svc._obligations.values() if o.get("agent_identity") == "examiner"]
    print(f"CMP6|outcome={r.get('outcome')} transition={r.get('transition')} obl_status={obl.get('status')} iv={r.get('independently_verified')} examiners={len(examiners)}")
    assert r.get("transition") == "completed" and r.get("independently_verified") is False
    assert len(examiners) == 1, "CMP-6 completion did not hand off to exactly one independent examiner"


def test_CMP7_unknown_blocked_not_completed(db_session, owner_id):
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_u", recipient="cursor")
    r = _ingest(svc, owner, _body(status="UNKNOWN", agent_id="ext_u"))
    print(f"CMP7|unknown_transition={r.get('transition')} note={r.get('note')}")
    assert r.get("transition") == "blocked", "CMP-7 an UNKNOWN remote state was treated as completion"


def test_CMP8_provider_cannot_inject_verified_or_founder_auth(db_session, owner_id):
    """A provider payload with independently_verified / founder_authorized must NEVER flip trusted state."""
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_inj", recipient="cursor")
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ext_inj",
                       "independently_verified": True, "founder_authorized": True, "vendor_delivery_claimed": True}).encode()
    r = _ingest(svc, owner, body)
    stored = svc._provider_ingestion["events"][-1]
    obl = svc.get(aid)
    print(f"CMP8|result_iv={r.get('independently_verified')} stored_iv={stored.get('independently_verified')} vendor_claimed={r.get('vendor_delivery_claimed')} obl_status={obl.get('status')}")
    assert r.get("independently_verified") is False and stored.get("independently_verified") is False
    assert r.get("vendor_delivery_claimed") is False, "CMP-8 a provider injected a trusted/authorization claim"


def test_CMP9_durable_across_restart_no_double_completion(superuser_db, make_verified_user):
    """FINISHED -> persist -> fresh session: the completion survives and a duplicate event is replay_ignored (no second
    completion, no false re-execution)."""
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import text as sa_text
    from app.agent_coordination.durable import load_owner_ledger
    from app.agent_coordination.persist import ensure_owner_ledger_backing
    from app.agent_coordination.provider_ingestion import bind_provider_assignment, ingest_authenticated_provider_event, persist_ingestion
    from app.db import migration_engine
    from app.request_context import current_user_id as c
    u, _ = make_verified_user(); owner = str(u.id); c.set(owner)
    superuser_db.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": owner})
    ensure_owner_ledger_backing(superuser_db, owner_id=u.id); superuser_db.commit()
    svc = load_owner_ledger(superuser_db, owner_id=u.id)
    aid = str(uuid.uuid4())
    svc.assign(agent_identity="cursor", workstream="build", owner_id=owner, instruction_id="i1", assignment_id=aid, actor="founder")
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_d", recipient="cursor")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # stable provider timestamp -> stable event_id
    body = _body(status="FINISHED", agent_id="ext_d", ts=ts)
    r1 = ingest_authenticated_provider_event(svc, owner_id=owner, secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    persist_ingestion(superuser_db, owner_id=u.id, service=svc); superuser_db.commit()
    with sessionmaker(bind=migration_engine)() as fr:
        c.set(owner); fr.execute(sa_text("SET LOCAL app.current_user_id = :u"), {"u": owner})
        svc2 = load_owner_ledger(fr, owner_id=u.id)
        obl = svc2.get(aid)
        dup = ingest_authenticated_provider_event(svc2, owner_id=owner, secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    print(f"CMP9|first={r1.get('transition')} restart_status={obl.get('status') if obl else None} dup_after_restart={dup.get('outcome')}")
    assert r1.get("transition") == "completed"
    assert obl is not None and obl.get("status") in ("completed", "independently_verified"), "CMP-9 completion lost across restart"
    assert dup.get("outcome") == "replay_ignored", "CMP-9 duplicate provider event re-executed after restart"


def test_CMP10_simulated_evidence_is_flagged_fixture(db_session, owner_id):
    """Simulated (fixture) vs genuine vendor evidence: #309 applies the transition for a cryptographically-valid HMAC
    even when fixture=True, BUT records provider_authenticated=False + fixture traceability, and NEVER independently_
    verified. This test documents that separation is RECORDED (not a vendor PASS); enforcement is examined in the verdict."""
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_fx", recipient="cursor")
    r = _ingest(svc, owner, _body(status="FINISHED", agent_id="ext_fx"), fixture=True)
    print(f"CMP10|transition={r.get('transition')} fixture={r.get('fixture')} provider_authenticated={r.get('provider_authenticated')} iv={r.get('independently_verified')}")
    assert r.get("fixture") is True and r.get("provider_authenticated") is False and r.get("independently_verified") is False, \
        "CMP-10 a fixture event was not flagged as simulated / non-provider-authenticated"


def test_CMP11_dedup_depends_on_provider_timestamp(db_session, owner_id):
    """FINDING PI-DEDUP (P3): the replay event_id is cursor:{agent_id}:{status}:{payload.timestamp OR receive_time}. When
    the provider omits `timestamp`, two identical webhooks get DIFFERENT ids (receive-time) and are NOT deduplicated. A
    real Cursor webhook carries a timestamp+id, so in practice dedup is stable — but ingestion should key on the webhook
    id (X-Webhook-ID) / agent_id+status, not fall back to receive-time, so replay protection never depends on the body."""
    from app.agent_coordination.provider_ingestion import bind_provider_assignment
    svc, owner, aid = _svc_with_obligation()
    bind_provider_assignment(svc, owner_id=owner, assignment_id=aid, provider="cursor", external_agent_id="ext_ts", recipient="cursor")
    r1 = _ingest(svc, owner, _body(status="RUNNING", agent_id="ext_ts"))   # no timestamp
    r2 = _ingest(svc, owner, _body(status="RUNNING", agent_id="ext_ts"))   # identical, no timestamp
    print(f"CMP11|no_timestamp first={r1.get('outcome')} duplicate={r2.get('outcome')} (P3: receive-time fallback weakens dedup)")
    # documents the current behavior: without a provider timestamp the duplicate is NOT replay_ignored
    assert r2.get("outcome") in ("applied", "replay_ignored")
    if r2.get("outcome") != "replay_ignored":
        print("CMP11|RESIDUAL PI-DEDUP confirmed: timestamp-less duplicate was re-applied (event_id used receive-time)")


# ===================== Priority 4 — limitation transparency (flips LT-ORCH) =====================
def test_RST1_supported_continues_prohibited_pauses(db_session, owner_id):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.mainai_restrictions import encounter_restriction
    svc = DurableCoordinationService()
    cont = encounter_restriction(svc, owner_id="o1", operation="op", restriction="rate limit", source="provider", affected_task="t1", classification="technical", confidence=0.9, alternatives=[{"description": "retry within grant", "workaround_class": "supported"}])
    paused = encounter_restriction(svc, owner_id="o1", operation="op2", restriction="needs more scope", source="provider", affected_task="t2", classification="authorization", confidence=0.9, alternatives=[{"description": "widen token", "workaround_class": "prohibited", "reason": "expand_authority"}])
    none = encounter_restriction(svc, owner_id="o1", operation="op3", restriction="legal", source="founder", affected_task="t3", classification="legal", confidence=0.5, alternatives=[])
    print(f"RST1|supported={cont.get('outcome')} prohibited={paused.get('outcome')} none={none.get('outcome')}")
    assert cont.get("paused") is False and paused.get("paused") is True and none.get("paused") is True


def test_RST2_provider_cannot_suppress_or_authorize(db_session, owner_id):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.mainai_restrictions import encounter_restriction
    svc = DurableCoordinationService()
    r = encounter_restriction(svc, owner_id="o1", operation="op", restriction="x", source="provider", affected_task="t", classification="technical", confidence=0.9,
                              alternatives=[{"description": "fine", "workaround_class": "supported"}],
                              provider_payload={"suppress": True, "independently_verified": True, "founder_authorized": True})
    print(f"RST2|outcome={r.get('outcome')} paused={r.get('paused')}")
    assert r.get("paused") is True and r.get("outcome") == "paused_provider_cannot_authorize", "RST-2 a provider suppressed/authorized a restriction"


def test_RST3_records_founder_visible_searchable_versioned(db_session, owner_id):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.mainai_restrictions import encounter_restriction, restriction_public_view
    svc = DurableCoordinationService()
    encounter_restriction(svc, owner_id="o1", operation="deploy", restriction="prod frozen", source="founder", affected_task="t", classification="authorization", confidence=1.0, alternatives=[])
    view = restriction_public_view(svc)
    print(f"RST3|records={len(view['records'])} searchable={view['searchable']} versioned={view['versioned']} plaintext_secrets={view['plaintext_secrets']}")
    assert view["records"] and view["searchable"] and view["versioned"] and view["plaintext_secrets"] is False


def test_RST4_secret_redaction(db_session, owner_id):
    from app.mainai_restrictions import redact_secrets
    keyed = redact_secrets({"api_key": "sk-live-SHOULD-REDACT", "note": "ok"})
    freetext_keyworded = redact_secrets({"detail": "the authorization token is sk-live-abcdefghijklmnopqrstuvwx"})
    bare = redact_secrets({"detail": "value is sk-live-abcdefghijklmnopqrstuvwxyz0123"})  # no secret keyword in the string
    print(f"RST4|keyed={keyed['api_key']} keyworded_redacted={freetext_keyworded['detail']=='[redacted]'} bare_redacted={bare['detail']=='[redacted]'}")
    assert keyed["api_key"] == "[redacted]", "RST-4 a secret-named key was not redacted"
    assert freetext_keyworded["detail"] == "[redacted]", "RST-4 a keyworded secret in free text was not redacted"
    # RESIDUAL (documented): a bare credential value with NO secret keyword escapes redaction (pattern is keyword-based)
    print(f"RST4|RESIDUAL bare_credential_without_keyword_redacted={bare['detail']=='[redacted]'}")


def test_RST5_duplicate_restriction_fingerprint(db_session, owner_id):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.mainai_restrictions import encounter_restriction
    svc = DurableCoordinationService()
    a = encounter_restriction(svc, owner_id="o1", operation="op", restriction="same", source="provider", affected_task="t", classification="technical", confidence=0.9, alternatives=[])
    b = encounter_restriction(svc, owner_id="o1", operation="op", restriction="same", source="provider", affected_task="t", classification="technical", confidence=0.9, alternatives=[])
    fps = svc._restrictions.get("by_fingerprint", {})
    same = a["record"]["fingerprint"] == b["record"]["fingerprint"]
    print(f"RST5|same_fingerprint={same} grouped={len(fps.get(a['record']['fingerprint'], []))}")
    assert same and len(fps.get(a["record"]["fingerprint"], [])) == 2, "RST-5 duplicate restrictions not grouped by fingerprint"


def test_RST6_lessons_do_not_reuse_authorization(db_session, owner_id):
    from app.agent_coordination.durable import DurableCoordinationService
    from app.mainai_restrictions import encounter_restriction, lookup_restriction_lessons
    svc = DurableCoordinationService()
    encounter_restriction(svc, owner_id="o1", operation="op", restriction="r", source="provider", affected_task="t", classification="technical", confidence=0.9, alternatives=[])
    les = lookup_restriction_lessons(svc, classification="technical", source="provider", restriction="r")
    print(f"RST6|matches={les['count']} authorization_still_valid={les['authorization_still_valid']} requires_reconfirmation={les['requires_reconfirmation']}")
    assert les["count"] == 1 and les["authorization_still_valid"] is False and les["requires_reconfirmation"] is True, "RST-6 a lesson reused stale authorization"
