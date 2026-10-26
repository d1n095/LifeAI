"""#314 composed MainAI operating cycle — independent examination (examiner-owned).

Subject: #314 product SHA 2308b697 (= #312 provider corrections + #310 absence package + new
app.agent_coordination.autonomous_cycle.run_authorized_operating_cycle + SPEND-STOP fix in
provider_spend). Drives the REAL composed cycle.

CENTRAL DISTINCTION (Founder-directed): run_authorized_operating_cycle is ONE TICK that ingests a
FIXTURE provider event (fixture=True hardcoded) and reports vendor_delivery_claimed=False. It is NOT
a live vendor round trip and NOT a running production loop (no scheduler/while). These tests assert
exactly that, and never report fixture success as a completed live round trip.

No live/paid calls (egress guard). first_cycle_go=false.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "whsec_fc314"
UA = "Cursor-Agent-Webhook/1.0"


def _sig(b): return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()
def _body(status="FINISHED", agent_id="bc_cycle_agent", ts=None):
    p = {"event": "statusChange", "status": status, "id": agent_id}
    if ts: p["timestamp"] = ts
    return json.dumps(p).encode()


def _cycle(db, owner, *, with_provider=True, absence_enabled=False, capability="public_text_classify",
           standing_grants=None, body=None):
    from app.agent_coordination.autonomous_cycle import run_authorized_operating_cycle
    from app.agent_coordination.durable import DurableCoordinationService
    svc = DurableCoordinationService()
    kw = dict(owner_id=str(owner), goal_text="composed cycle probe", required_capability=capability,
              absence_enabled=absence_enabled, standing_grants=standing_grants)
    if with_provider:
        b = body if body is not None else _body(ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        kw.update(provider_secret=SECRET, provider_raw_body=b, provider_signature=_sig(b), provider_user_agent=UA)
    return svc, run_authorized_operating_cycle(db, svc, **kw)


def _seed_agents(db, owner):
    from app.workforce.first_team import bootstrap_first_team
    from app.workforce.first_delegation_scenario import _seed_research_candidates
    bootstrap_first_team(db, owner_id=owner)
    _seed_research_candidates(db, owner)


# ---------------------------------------------------------------------------
# A. The cycle is a fixture TICK, not a live vendor round trip (truthful reporting)
# ---------------------------------------------------------------------------

def test_FC314_A1_tick_is_fixture_not_live_round_trip(db_session, owner_id, egress):
    svc, r = _cycle(db_session, owner_id)
    assert r["outcome"] == "cycled", r
    assert r["vendor_delivery_claimed"] is False and r["live_paid_mutate"] is False
    assert r["first_cycle_go"] is False
    # the provider event was a fixture; no live paid call / egress occurred
    assert egress.provider_calls == [] and egress.sockets == []
    # provider-reported completion is NOT independent verification
    assert r["independently_verified"] is False


# ---------------------------------------------------------------------------
# B. STOP + authority enforcement at the cycle boundary
# ---------------------------------------------------------------------------

def test_FC314_B1_stop_halts_cycle_no_assignment(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop")
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)              # positive control
    svc, r = _cycle(db_session, owner_id, absence_enabled=True)
    assert r["outcome"] != "cycled", f"cycle proceeded under STOP: {r}"
    assert r.get("absence", {}).get("stop_blocked") is True
    assert r.get("catchup", {}).get("silence_was_not_treated_as_approval") is True


# ---------------------------------------------------------------------------
# C. SPEND-STOP — no new spending reservations during active STOP
# ---------------------------------------------------------------------------

def test_FC314_C1_no_spend_reservation_under_stop(db_session, owner_id):
    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
    from app.mainai_execution.planner import create_goal
    from app.provider_spend import authorize_provider_spend, reserve_provider_spend_call, ProviderSpendError
    from app.workforce.kill_switch import activate_kill_switch
    goal = create_goal(db_session, owner_id=owner_id, title="spend", original_instruction="i", created_by="t")
    db_session.flush()
    prop = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"p-{uuid.uuid4()}")
    _, env = authorize_execution_scope(db_session, owner_id=owner_id, proposal_id=prop.id, authorized_by="founder",
                                       authorized_paths=["README.md"], authorized_capabilities=["read_file"],
                                       authorized_risk="low", envelope_idempotency_key=f"e-{uuid.uuid4()}")
    authorize_provider_spend(db_session, owner_id=owner_id, goal_id=goal.id, execution_envelope_id=env.id,
                             authorized_by="founder", max_cost_usd=Decimal("1.00"), max_requests=5,
                             max_cost_per_request_usd=Decimal("0.25"), idempotency_key=f"g-{uuid.uuid4()}",
                             allowed_providers=["fake"], allowed_models=["fake-plan-v1"])
    # STOP now active -> reservation must be refused (SPEND-STOP fix)
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop before spend")
    with pytest.raises(ProviderSpendError) as ei:
        reserve_provider_spend_call(db_session, owner_id=owner_id, goal_id=goal.id,
                                    source_ref=f"r-{uuid.uuid4()}", provider="fake", model="fake-plan-v1")
    assert "stop" in str(ei.value).lower() or "authority" in str(ei.value).lower(), ei.value


# ---------------------------------------------------------------------------
# D. Capability-based selection is wired (real selector) — with a documented broker-bypass finding
# ---------------------------------------------------------------------------

def test_FC314_D1_FINDING_selector_namespace_breaks_assignment(db_session, owner_id):
    """FINDING SELECTOR-ASSIGN-NAMESPACE (P2): when a real workforce agent matches the capability,
    the cycle's select_best_candidate returns its workforce `agent_key` (e.g. 'research-strong'),
    which service.assign then REJECTS as 'unknown agent identity' — the coordination ledger only
    accepts known recipients (cursor/claude/codex). So wiring the real selector BREAKS the tick; the
    cycle only 'cycles' when the selector FAILS (SelectorError) and falls back to the hardcoded
    CAPABILITY_TO_AGENT default. Capability-based delegation through workforce services is therefore
    NOT functional end-to-end in #314, and DELEGATE-WIRING is not genuinely closed for real agents."""
    _seed_agents(db_session, owner_id)  # now 'public_text_classify' has workforce candidates
    svc, r = _cycle(db_session, owner_id, capability="public_text_classify")
    assert r["outcome"] == "assign_rejected", f"expected the namespace mismatch; got {r.get('outcome')}"
    assert "unknown agent identity" in str(r.get("reason", "")).lower(), r


def test_FC314_D2_cycle_only_completes_on_capability_default_fallback(db_session, owner_id):
    """Corollary: with NO workforce agent for the capability, the selector raises and the cycle falls
    back to a coordination recipient (cursor) and completes the fixture tick. Selection is 'wired' but
    only the fallback path reaches an assignment — see SELECTOR-ASSIGN-NAMESPACE."""
    # do NOT seed agents -> SelectorError -> capability default
    svc, r = _cycle(db_session, owner_id, capability="public_text_classify")
    assert r["outcome"] == "cycled" and r["selector"]["note"] == "no_workforce_agent_use_capability_default"
    assert r["selector"]["agent"] == "cursor"


# ---------------------------------------------------------------------------
# E. Independent verification before verification-required continuation
# ---------------------------------------------------------------------------

def test_FC314_E1_completion_is_not_independently_verified_without_attestation(db_session, owner_id):
    svc, r = _cycle(db_session, owner_id)
    assert r["outcome"] == "cycled"
    # a fixture FINISHED completed the obligation but ICV gate keeps independently_verified False
    assert r["independently_verified"] is False
    v = r.get("verification") or {}
    assert v.get("outcome") == "verification_required" or v.get("independently_verified") is False, v


# ---------------------------------------------------------------------------
# F. Founder absence without invented authorization
# ---------------------------------------------------------------------------

def test_FC314_F1_absence_defers_new_highrisk_without_grant(db_session, owner_id):
    from app.mainai_operational_handover.absence import evaluate_absence_tick
    ev = evaluate_absence_tick(items=[{"id": "x", "risk": "deploy", "approved": True, "new_work": True, "scope": "deploys"}],
                               stop_blocked=False, absence_enabled=True, standing_grants=[])
    assert not ev["continued"]
    assert ev["silence_is_not_approval"] is True


# ---------------------------------------------------------------------------
# G. Durability + duplicate-effect prevention
# ---------------------------------------------------------------------------

def test_FC314_G1_duplicate_fixture_event_replay_ignored_and_state_durable(db_session, owner_id):
    body = _body(ts=None)  # timestamp-less -> stable id derives from raw_body
    svc, r1 = _cycle(db_session, owner_id, body=body)
    # second ingest of the SAME body on the same service is deduped
    from app.agent_coordination.provider_ingestion import ingest_authenticated_provider_event
    r2 = ingest_authenticated_provider_event(svc, owner_id=str(owner_id), secret=SECRET, raw_body=body,
                                             signature=_sig(body), user_agent=UA, fixture=True, db=db_session)
    assert r2.get("outcome") == "replay_ignored", r2
    dumped = svc.dump_for(r1["assignment_id"])
    assert "operational_handover" in dumped
