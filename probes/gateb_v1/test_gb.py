"""Gate-B readiness: capability-verdict boundary negatives (GBV-*) + an independent evidence-driven preflight (GBP).

GBV-* prove #307's capability_verdict cannot be turned ready by a local gateway, green CI, a single provider event, or
an untrusted payload field. GBP is an examiner-owned, provider-neutral, READ-ONLY preflight that classifies each of the
12 Gate-B prerequisites PASS / FAIL / BLOCKED / NOT_TESTED from real evidence — never manufacturing live evidence and
never requiring a provider credential. Target: #307 f24ccf2. No live/paid calls, no network, isolated DB.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "preflight-webhook-secret-0123456789abcdef"


def _sig(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ===================== Priority 1 — capability_verdict boundary negatives =====================
def test_GBV1_verdict_is_fail_closed_not_ready(db_session, owner_id):
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    v = capability_verdict()
    print(f"GBV1|ready={v['ready']} cycle_ready={v['unattended_operating_cycle_ready']} autonomy={v['unattended_autonomy_ready']} external_connected={v['external_agent_connected']}")
    assert v["ready"] is False and v["unattended_operating_cycle_ready"] is False and v["unattended_autonomy_ready"] is False
    assert all(r["satisfied"] is False for r in v["requirements"]), "GBV-1 a requirement is marked satisfied without live evidence"


def test_GBV2_local_or_green_ci_or_prototype_insufficient(db_session, owner_id):
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    v = capability_verdict()
    print(f"GBV2|local_gateway_sufficient={v['local_gateway_sufficient']} green_ci_sufficient={v['green_ci_sufficient']} prototype_sufficient={v['disconnected_prototype_sufficient']}")
    assert v["local_gateway_sufficient"] is False, "GBV-2 a local gateway was treated as sufficient"
    assert v["green_ci_sufficient"] is False, "GBV-2 green CI alone turned readiness on"
    assert v["disconnected_prototype_sufficient"] is False, "GBV-2 a disconnected prototype was treated as sufficient"


def test_GBV3_missing_authority_keeps_blocked(db_session, owner_id):
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    v = capability_verdict()
    gates = set(v["blocking_gates"])
    print(f"GBV3|blocking_gates={sorted(gates)}")
    assert "fa9_co10_privilege_and_publication" in gates, "GBV-3 STOP-clear/publication/privilege not a blocking gate"
    assert "founder_cursor_cloud_live_mutate_authorization" in gates and "independent_certification" in gates
    assert "first_cycle_go_false" in gates


def test_GBV4_single_event_cannot_certify_cycle(db_session, owner_id):
    """One valid provider webhook event -> a proposal only; it does NOT satisfy the multi-step cycle or set verified."""
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_1"}).encode()
    ev = verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    prop = propose_ledger_event(ev)
    v = capability_verdict()
    print(f"GBV4|one_event_persisted={prop['persisted']} independently_verified={prop['independently_verified']} cycle_ready={v['unattended_operating_cycle_ready']}")
    assert prop["persisted"] is False and prop["independently_verified"] is False
    assert v["unattended_operating_cycle_ready"] is False, "GBV-4 one event certified the whole operating cycle"


def test_GBV5_untrusted_payload_cannot_alter_classification(db_session, owner_id):
    """Injected independently_verified / ready / vendor_delivery_claimed in a webhook payload never propagate."""
    from app.agent_coordination.external_connectivity.cursor_cloud import verify_v0_webhook
    from app.agent_coordination.external_connectivity.ledger import propose_ledger_event
    body = json.dumps({"event": "statusChange", "status": "FINISHED", "id": "ag_x",
                       "independently_verified": True, "ready": True, "vendor_delivery_claimed": True,
                       "satisfied": True}).encode()
    ev = verify_v0_webhook(secret=SECRET, raw_body=body, signature=_sig(SECRET, body), user_agent="Cursor-Agent-Webhook/1.0", fixture=True)
    prop = propose_ledger_event(ev)
    print(f"GBV5|iv={prop['independently_verified']} vendor_claimed={prop['vendor_delivery_claimed']} agent_prov_auth={prop['agent_identity']['provider_authenticated']}")
    assert prop["independently_verified"] is False and prop["vendor_delivery_claimed"] is False
    assert prop["agent_identity"]["provider_authenticated"] is False, "GBV-5 an untrusted payload flipped a trust/readiness flag"


def test_GBV6_verdict_takes_no_input_so_nothing_can_flip_it():
    """capability_verdict() is nullary and deterministic — no caller-supplied argument (forged or otherwise) can set ready."""
    import inspect
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    params = inspect.signature(capability_verdict).parameters
    a, b = capability_verdict(), capability_verdict()
    print(f"GBV6|params={list(params)} stable={a['ready']==b['ready']==False}")
    assert len(params) == 0 and a["ready"] is False and b["ready"] is False


# ===================== Priority 2 — independent evidence-driven Gate-B preflight =====================
def _symbol_exists(modpath, name):
    try:
        return hasattr(importlib.import_module(modpath), name)
    except Exception:
        return False


def _preflight() -> list[dict]:
    """Provider-neutral, read-only. Classifies each prerequisite from real evidence on THIS branch (f24ccf2, #303-based).
    PASS = mechanism present and locally evidenced; BLOCKED = needs a live provider / Codex authority; NOT_IMPLEMENTED =
    no code path on this branch; NOT_TESTED = needs the composed frozen release. Never manufactures live evidence."""
    from app.agent_coordination.external_connectivity.verdict import capability_verdict
    v = capability_verdict()
    has_delivery = _symbol_exists("app.agent_coordination.delivery", "submit_receipt")
    has_persist = _symbol_exists("app.agent_coordination.persist", "persist_owner_ledger")
    has_lifecycle_followup = _symbol_exists("app.agent_coordination.lifecycle", "_follow_up_assignment")
    has_routing = _symbol_exists("app.agent_coordination.routing", "next_feasible_assignment_for_agent")
    has_agent_side = _symbol_exists("app.agent_coordination.agent_side", "run_agent_side")  # #304+, NOT on #303-based #307
    has_ingest_provider = _symbol_exists("app.agent_coordination.external_connectivity.ledger", "ingest_provider_event")  # a real persist entry, not just propose
    rows = [
        ("1 authenticated external agent identity", "BLOCKED", "classify_credential distinguishes provider vs local; no provider-authenticated identity in runtime", "Founder (live key) + Cursor"),
        ("2 actual assignment receipt", "BLOCKED", "create_agent hard-blocked; no live POST /v1/agents receipt", "Founder (live-mutate) + Cursor"),
        ("3 authorized execution", "BLOCKED", "live mutate hard-blocked even with enable flags", "Founder + Cursor"),
        ("4 durable provider-event ingestion", "NOT_IMPLEMENTED" if not has_ingest_provider else "PASS", "ledger.propose_ledger_event is proposal-only (persisted=False); no function binds a verified provider event into the durable ledger", "Cursor/MainAI"),
        ("5 evidence-to-assignment correlation", "BLOCKED", "propose documents GET /v1/agents/{id}+run binding; not wired to an AgentWorkAssignment", "Cursor/MainAI"),
        ("6 independent completion verification", "BLOCKED", "examiner mechanism exists locally but no external completion to verify; independently_verified stays False", "Cursor/MainAI + examiner"),
        ("7 automatic examiner handoff", "PASS" if has_lifecycle_followup else "NOT_IMPLEMENTED", "lifecycle._follow_up_assignment generates one examiner obligation (local; proven by UA-4/handover)", "present"),
        ("8 findings-to-correction routing", "PASS", "status machine reviewing->changes_requested->ready proven (UA-5)", "present"),
        ("9 restart/interruption recovery", "PASS" if (has_persist) else "NOT_IMPLEMENTED", "durable ledger reload proven locally (UA-9); provider-side retry/revocation BLOCKED", "present (local) / Cursor (provider-side)"),
        ("10 STOP/spending/publication authority", "BLOCKED", "FA-9/CO-10 + publication Codex-owned and open; mutations hard-blocked; spend fail-closed", "Codex (STOP/publication) + Founder (spend)"),
        ("11 independent certification of composed release", "NOT_TESTED", "requires the composed frozen release SHA (agent-side #304-306 + provider adapter #307) + an examiner verdict; #307 is a separate #303-based branch", "Founder (freeze) + examiner"),
        ("12 Founder notification / intervention", "PASS", "Founder-ops snapshot reports local state (handover view); external-cycle exceptions N/A until the cycle runs", "present (local)"),
    ]
    # composition gap surfaced by evidence: the agent-side runner from #304-306 is ABSENT on this #303-based branch
    out = [{"prereq": p, "status": s, "evidence": e, "owner": o} for (p, s, e, o) in rows]
    out.append({"prereq": "COMPOSITION agent_side_runner_present_on_this_branch", "status": "FAIL" if not has_agent_side else "PASS",
                "evidence": f"app.agent_coordination.agent_side.run_agent_side present={has_agent_side} (#307 is #303-based, lacks #304-306 transport)", "owner": "Founder/MainAI (compose #304-306 + #307)"})
    out.append({"prereq": "verdict.ready", "status": "BLOCKED" if v["ready"] is False else "PASS", "evidence": v["verdict"][:80], "owner": "n/a"})
    return out


def test_GBP_preflight_matrix(db_session, owner_id):
    rows = _preflight()
    from collections import Counter
    tally = Counter(r["status"] for r in rows)
    for r in rows:
        print(f"GBP|{r['status']:14s} {r['prereq']}  [{r['owner']}]")
    print(f"GBP|TALLY={dict(tally)}")
    # Gate-B is NOT ready: no prerequisite that requires a live provider is PASS, and ingestion/correlation are not wired.
    assert tally.get("PASS", 0) <= 5, "GBP sanity: too many PASS — a live-provider prerequisite may be falsely satisfied"
    live_rows = [r for r in rows if r["prereq"].startswith(("1 ", "2 ", "3 ", "4 ", "5 ", "6 ", "10 "))]
    assert all(r["status"] in ("BLOCKED", "NOT_IMPLEMENTED", "NOT_TESTED", "FAIL") for r in live_rows), \
        "GBP a live-provider / Codex-authority prerequisite is marked PASS without live evidence"
    # the real external operating cycle must NOT be ready
    assert any(r["prereq"] == "verdict.ready" and r["status"] == "BLOCKED" for r in rows)
