"""Priority 1 — Durable Provider-Event Ingestion acceptance (PEVI-*), test-first.

Required chain: verified provider event -> correct owner/assignment -> durable lifecycle evidence -> independent review
-> authorized continuation. On #307 f24ccf2 the ingestion/binding surface does NOT exist (ledger.propose_ledger_event is
proposal-only; no ingest_provider_event). These tests assert what CAN be proven today (authentication, event-id
determinism, provider-reported != independently-verified) as PASS, and mark the missing chain legs NOT_IMPLEMENTED /
BLOCKED with executable NEGATIVE controls — never a vendor PASS on a local fixture. They flip to PASS when Cursor wires
the ingestion surface described in fc307/COMPOSITION_CONTRACT.md. No live/paid calls, no network, isolated DB.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "pevi-webhook-secret-0123456789abcdef"


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _event(status="FINISHED", agent_id="ag_1", fixture=True):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    body = json.dumps({"event": "statusChange", "status": status, "id": agent_id}).encode()
    return verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=fixture)


def _has_ingest():
    try:
        return hasattr(importlib.import_module("app.agent_coordination.external_connectivity.ledger"), "ingest_provider_event")
    except Exception:
        return False


# PEVI-1 authentication — an unauthenticated/invalid-HMAC event never enters the chain
def test_PEVI1_authentication(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_1"}).encode()
    bad = verify_v0_webhook(secret=SECRET, raw_body=body, signature="sha256=00", user_agent="Cursor-Agent-Webhook/1.0")
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    prop = propose_ledger_event(bad)
    print(f"PEVI1|PASS bad_hmac={bad.get('outcome')} proposed={prop['proposed']} persisted={prop['persisted']}")
    assert bad.get("outcome") == "rejected" and prop["proposed"] is False and prop["persisted"] is False


# PEVI-2 identity correlation — binding provider agent.id <-> owner/assignment is NOT_IMPLEMENTED
def test_PEVI2_identity_correlation_not_implemented(db_session, owner_id):
    print(f"PEVI2|NOT_IMPLEMENTED ingest_provider_event_present={_has_ingest()}")
    assert _has_ingest() is False, ("PEVI-2 ingest_provider_event now EXISTS — wire the owner/assignment binding test "
                                    "(flip to assert a verified event binds to exactly one AgentWorkAssignment under the right owner)")


# PEVI-3 webhook replay — deterministic event_id enables dedup; no double-persist is possible today
def test_PEVI3_replay_event_id_deterministic(db_session, owner_id):
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    e1, e2 = propose_ledger_event(_event(agent_id="ag_dup")), propose_ledger_event(_event(agent_id="ag_dup"))
    print(f"PEVI3|PASS same_event_id={e1['event']['event_id']==e2['event']['event_id']} persisted={e1['persisted']}")
    assert e1["event"]["event_id"] == e2["event"]["event_id"] and e1["persisted"] is False


# PEVI-4 event reordering — monotonic/no-regression at ingestion is NOT wired for provider events
def test_PEVI4_reordering_not_wired():
    pytest.skip("NOT_IMPLEMENTED/BLOCKED: provider-event reordering safety requires ingestion into the durable ledger "
                "(status-rank). No ingest_provider_event exists. Substrate behavior proven at the ledger by CR-13/SUB-02.")


# PEVI-5 concurrent ingestion — no ingestion surface to race
def test_PEVI5_concurrent_ingestion_not_wired():
    pytest.skip("NOT_IMPLEMENTED: concurrent provider-event ingestion cannot be exercised; no ingestion surface exists. "
                "Concurrent DURABLE persistence is proven at the ledger by delivery/lifecycle (DLV/CR); provider binding TBD.")


# PEVI-6 stale authorization — a valid HMAC does NOT imply an authorized (non-revoked) provider key
def test_PEVI6_valid_hmac_is_not_authorization(db_session, owner_id):
    """A webhook HMAC authenticates ORIGIN, not that the provider key is still authorized. A verified event must not
    become a persisted completion on its own — ingestion must re-check authorization (NOT_IMPLEMENTED)."""
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    ev = _event(agent_id="ag_stale", fixture=True)
    prop = propose_ledger_event(ev)
    print(f"PEVI6|PASS hmac_accepted={ev.get('outcome')} provider_authenticated={prop['agent_identity']['provider_authenticated']} persisted={prop['persisted']}")
    assert ev.get("outcome") == "accepted" and prop["agent_identity"]["provider_authenticated"] is False and prop["persisted"] is False


# PEVI-7 cross-owner contamination — no owner binding at ingestion
def test_PEVI7_cross_owner_binding_not_wired():
    pytest.skip("NOT_IMPLEMENTED/BLOCKED: a verified provider event carries no owner context; cross-owner isolation at "
                "ingestion needs the owner/assignment binding surface. Enforced for the local gateway by RAI-07/CS-6.")


# PEVI-8 worker crash / persistence / recovery — no provider event is persisted to recover
def test_PEVI8_no_durable_provider_evidence_to_recover(db_session, owner_id):
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    prop = propose_ledger_event(_event(agent_id="ag_recover"))
    print(f"PEVI8|NOT_IMPLEMENTED persisted={prop['persisted']} reuse_persist_fn={prop.get('reuse',{}).get('persist','')[:40]}")
    assert prop["persisted"] is False, ("PEVI-8 a provider event is now persisted — add the restart-recovery assertion "
                                        "(verified completion survives a fresh session, idempotent on the provider event id)")


# PEVI-9 provider-reported completion != independently verified
def test_PEVI9_provider_reported_is_not_independently_verified(db_session, owner_id):
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    prop = propose_ledger_event(_event(status="FINISHED", agent_id="ag_done"))
    v = capability_verdict()
    print(f"PEVI9|PASS proposed_delivery={prop['completion_evidence']['proposed_delivery_status']} independently_verified={prop['independently_verified']} external_connected={v['external_agent_connected']}")
    assert prop["independently_verified"] is False, "PEVI-9 a provider-reported FINISHED was treated as independently verified"
    assert v["external_agent_connected"] is False
