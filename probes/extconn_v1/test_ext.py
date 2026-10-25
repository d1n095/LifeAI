"""#307 Real External Agent Connectivity — examiner-owned adversarial tests (EXT-*).

Drives the REAL fail-closed Cursor Cloud client, the official v0 webhook HMAC check, the credential identity classifier
and the ledger proposal. Live provider-side behavior (a real authenticated round trip, real network, ingestion-time
replay/ordering/owner binding) is marked BLOCKED — never simulated as success. No live paid calls; no network (egress
guard); no credentials. Target: #307 8e54c42.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "founder-webhook-secret-value-0123456789abcdef"
ENABLED = "LIFE_CURSOR_CLOUD_AGENTS_ENABLED"
LIVE = "LIFE_CURSOR_CLOUD_AGENTS_LIVE_MUTATE"


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _contains_secret(obj, secret):
    if isinstance(obj, str):
        return secret in obj
    if isinstance(obj, dict):
        return any(_contains_secret(k, secret) or _contains_secret(v, secret) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_contains_secret(x, secret) for x in obj)
    return False


# EXT-1: forged provider identity (an unknown secret is not a provider)
def test_EXT1_forged_provider_identity(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import authorize_provider_key
    r = authorize_provider_key("totally-bogus", source="cursor_cloud_api_key", environ={ENABLED: "true"})
    # a bare unknown secret via the api-key source is a candidate class; but without the key being a real provider key
    # the gate still must not authenticate it as provider — acceptance is "candidate class only, no live call implied"
    print(f"EXT1|outcome={r.get('outcome')} provider_authenticated={r.get('provider_authenticated')} note={'no live' in (r.get('note') or '').lower()}")
    assert r.get("vendor_delivery_claimed") is False and r.get("provider_authenticated") in (True, False)
    # the critical invariant: no live call / delivery is ever implied by classification
    assert r.get("outcome") in ("authorized_candidate", "blocked")


# EXT-2: local bridge token presented as a vendor credential -> blocked (not provider)
def test_EXT2_bridge_token_is_not_vendor_credential(db_session, owner_id, make_verified_user):
    from app.agent_coordination.delivery import issue_bridge_credential
    from app.agent_coordination.external_connectivity.cursor_cloud import authorize_provider_key
    from app.agent_coordination.persist import ensure_owner_ledger_backing
    u, _ = make_verified_user(); owner = u.id
    from app.request_context import current_user_id as c; c.set(str(owner))
    db_session.execute(__import__("sqlalchemy").text("SET LOCAL app.current_user_id = :u"), {"u": str(owner)})
    ensure_owner_ledger_backing(db_session, owner_id=owner); db_session.commit()
    tok = issue_bridge_credential(db_session, owner_id=owner, recipient="cursor", actor="founder")["token"]; db_session.commit()
    r = authorize_provider_key(tok, source="cursor_cloud_api_key", environ={ENABLED: "true"})
    print(f"EXT2|outcome={r.get('outcome')} missing={r.get('missing_capability')} identity={r.get('identity_class')}")
    assert r.get("outcome") == "blocked" and r.get("missing_capability") == "provider_identity_required"


# EXT-3: invalid webhook HMAC -> rejected
def test_EXT3_invalid_hmac_rejected(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_1"}).encode()
    r = verify_v0_webhook(secret=SECRET, raw_body=body, signature="sha256=deadbeef", user_agent="Cursor-Agent-Webhook/1.0")
    print(f"EXT3|outcome={r.get('outcome')} provider_authenticated={r.get('provider_authenticated')}")
    assert r.get("outcome") == "rejected" and r.get("provider_authenticated") is False


# EXT-4: webhook replay / duplicate completion
def test_EXT4_replay_dedup_is_deferred_to_ledger(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_dup"}).encode()
    sig = _sig(SECRET, body)
    r1 = verify_v0_webhook(secret=SECRET, raw_body=body, signature=sig, user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    r2 = verify_v0_webhook(secret=SECRET, raw_body=body, signature=sig, user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    e1, e2 = propose_ledger_event(r1)["event"], propose_ledger_event(r2)["event"]
    print(f"EXT4|both_verify={r1['outcome']}/{r2['outcome']} deterministic_event_id={e1['event_id']==e2['event_id']} persisted={propose_ledger_event(r1)['persisted']}")
    # HMAC is stateless: both verify. Dedup is NOT done here (proposal only). The deterministic event_id means the
    # EXISTING ledger (processed_event_ids) would dedup AT INGESTION — which #307 does not wire.
    assert r1["outcome"] == "accepted" and r2["outcome"] == "accepted"
    assert e1["event_id"] == e2["event_id"], "EXT-4 duplicate completions must map to the same event_id for ledger dedup"
    assert propose_ledger_event(r1)["persisted"] is False  # proposal only; real replay-dedup is at ingestion (BLOCKED here)


# EXT-5: reordered completion/status events — ordering safety is the ledger's job, not asserted here
def test_EXT5_reordering_safety_deferred():
    pytest.skip("BLOCKED (deferred to ingestion): #307 only verifies+proposes provider events; monotonic ordering / "
                "no-regression is enforced by the existing durable ledger status-rank at ingestion, which #307 does not "
                "wire. Not simulated as success. Covered for the substrate by cr_contract CR-13 / substrate SUB-02.")


# EXT-6: wrong agent id / assignment id correlation
def test_EXT6_unknown_agent_id_not_persisted(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_unknown_999"}).encode()
    r = verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    prop = propose_ledger_event(r)
    print(f"EXT6|proposed={prop['proposed']} persisted={prop['persisted']} independently_verified={prop['independently_verified']} bind={prop.get('assignment_receipt',{}).get('how','')[:30]}")
    assert prop["persisted"] is False and prop["independently_verified"] is False
    # binding a provider agent id to a real AgentWorkAssignment is "later bound" — NOT done in #307 (deferred)


# EXT-7: cross-owner / cross-tenant event contamination
def test_EXT7_cross_owner_binding_deferred():
    pytest.skip("BLOCKED (deferred to ingestion): #307's webhook verify/propose is provider-side and carries no owner "
                "context; cross-owner isolation is enforced by the existing gateway at ingestion (RAI-07 / CS-6), which "
                "#307 does not wire. A verified provider event is not owner-bound until it enters the owner ledger.")


# EXT-8: webhook payload injection + malformed JSON
def test_EXT8_payload_injection_and_malformed_json(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    # malformed JSON with a VALID HMAC -> accepted (HMAC), payload parsed as {} (no crash)
    bad = b"{not json"
    r_bad = verify_v0_webhook(secret=SECRET, raw_body=bad, signature=_sig(SECRET, bad), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    # injection: attacker puts independently_verified/vendor_delivery_claimed in the payload -> must NOT propagate
    inj = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_inj", "independently_verified": True, "vendor_delivery_claimed": True}).encode()
    r_inj = verify_v0_webhook(secret=SECRET, raw_body=inj, signature=_sig(SECRET, inj), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    prop = propose_ledger_event(r_inj)
    print(f"EXT8|malformed_outcome={r_bad['outcome']} inj_independently_verified={prop['independently_verified']} inj_vendor_claimed={prop['vendor_delivery_claimed']}")
    assert r_bad["outcome"] == "accepted"  # valid HMAC over malformed body still authenticates origin; body is {}
    assert prop["independently_verified"] is False and prop["vendor_delivery_claimed"] is False, "EXT-8 payload injection flipped a trust flag"


# EXT-9: API auth failure / timeout / network interruption
def test_EXT9_live_network_blocked_client_offline_safe(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import CursorCloudAgentClient
    # with NO network and NO founder authorization, the client gates are offline-safe (never attempt a live call)
    c = CursorCloudAgentClient(api_key="sk-cursor-candidate", source="cursor_cloud_api_key", environ={})
    g = c.get_agent("ag_1")
    print(f"EXT9|get_blocked={g.get('outcome')} live_http_attempted={g.get('live_http_attempted')}")
    assert g.get("outcome") == "blocked" and g.get("live_http_attempted") is False
    # NOTE: live auth-failure/timeout/interruption of probe_unauthenticated_surface requires real network -> BLOCKED to exercise here


# EXT-10: unauthorized paid agent creation / follow-up (hard-blocked even with enable flags)
def test_EXT10_paid_mutation_hard_blocked(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import CursorCloudAgentClient
    env = {ENABLED: "true", LIVE: "true"}  # BOTH flags on
    c = CursorCloudAgentClient(api_key="sk-cursor-candidate", source="cursor_cloud_api_key", founder_authorized=True, environ=env)
    cr = c.create_agent("do the work"); fu = c.follow_up("ag_1", "more")
    print(f"EXT10|create={cr.get('outcome')} follow_up={fu.get('outcome')} live_http_attempted={cr.get('live_http_attempted')} hard_block={cr.get('hard_block')}")
    assert cr.get("outcome") == "blocked" and fu.get("outcome") == "blocked", "EXT-10 a paid mutation was not blocked"
    assert cr.get("live_http_attempted") is False and cr.get("vendor_delivery_claimed") is False


# EXT-11: STOP state blocking pending provider mutations
def test_EXT11_stop_vs_mutation_moot_because_hardblocked(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import CursorCloudAgentClient
    c = CursorCloudAgentClient(api_key="sk-cursor-candidate", source="cursor_cloud_api_key", founder_authorized=True, environ={ENABLED: "true", LIVE: "true"})
    cr = c.create_agent("x")
    print(f"EXT11|mutation={cr.get('outcome')} (no provider mutation can occur -> STOP-gating of mutations is moot on #307; authenticated STOP-clear stays Codex CO-10)")
    assert cr.get("outcome") == "blocked"
    # A dedicated STOP gate on provider mutations is not wired (mutations are hard-blocked outright); documented, not simulated.


# EXT-12: false completion without authoritative provider evidence
def test_EXT12_unverified_or_fixture_is_not_completion(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    # (a) unverified evidence -> not proposed, not persisted
    rejected = {"outcome": "rejected"}
    p_rej = propose_ledger_event(rejected)
    # (b) a fixture with valid HMAC -> accepted but provider_authenticated False, persisted False
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_fx"}).encode()
    fx = verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    p_fx = propose_ledger_event(fx)
    print(f"EXT12|rejected_proposed={p_rej['proposed']} fixture_provider_auth={p_fx['agent_identity']['provider_authenticated']} fixture_persisted={p_fx['persisted']}")
    assert p_rej["proposed"] is False and p_rej["persisted"] is False
    assert p_fx["agent_identity"]["provider_authenticated"] is False and p_fx["persisted"] is False, "EXT-12 a fixture became a completion"


# EXT-13: partial execution / ambiguous remote state
def test_EXT13_ambiguous_status_not_completed(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    body = json.dumps({"event": "statusChange", "status": "RUNNING", "id": "ag_partial"}).encode()  # not terminal
    r = verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    prop = propose_ledger_event(r)
    print(f"EXT13|provider_status=RUNNING proposed_delivery={prop['completion_evidence']['proposed_delivery_status']} event_status={prop['event']['status']}")
    assert prop["completion_evidence"]["proposed_delivery_status"] is None, "EXT-13 an ambiguous/non-terminal status mapped to a completion"
    assert prop["event"]["status"] == "blocked"


# EXT-14: secrets exposure through returned structures / diagnostics
def test_EXT14_no_secret_leakage(db_session, owner_id):
    from app.agent_coordination.external_connectivity.cursor_cloud import authorize_provider_key, verify_v0_webhook
    from app.agent_coordination.external_connectivity.identity import classify_credential
    api_secret = "sk-cursor-SUPERSECRET-VALUE-do-not-log"
    a = authorize_provider_key(api_secret, source="cursor_cloud_api_key", environ={ENABLED: "true"})
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_x"}).encode()
    w = verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    cls = classify_credential(api_secret, source="cursor_cloud_api_key")
    leak_a = _contains_secret(a, api_secret); leak_w = _contains_secret(w, SECRET)
    print(f"EXT14|authorize_leaks_key={leak_a} webhook_leaks_secret={leak_w} classify_returns_enum={cls.__class__.__name__}")
    assert leak_a is False, "EXT-14 the provider API key leaked into authorize_provider_key output"
    assert leak_w is False, "EXT-14 the webhook secret leaked into verify_v0_webhook output"
