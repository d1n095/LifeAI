"""Priority 3 — first-cycle integration acceptance gate for the CORRECTED autonomous-cycle candidate.

Test-first. Run NOW against the current best composed path (#314 2308b697) to establish the gate; the
same suite is the acceptance target for the next corrected successor of #314. Genuinely missing wiring
is marked FAIL/BLOCKED (strict xfail / skip), never PASS — so a correct successor flips it green.

Required end-state (Founder-directed):
  real workforce selector -> valid coordination recipient -> broker-issued assignment;
  no hardcoded vendor fallback masking selection failure;
  STOP/spend/verification throughout; durable recovery w/o duplicate assignments or external effects;
  no simulated provider event reported as genuine delivery; Founder available during background work.

To examine a corrected successor: `fc_firstcycle_v1/run.sh ../../wtcert-<newsha>`. first_cycle_go=false.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401

SECRET = "whsec_firstcycle"
UA = "Cursor-Agent-Webhook/1.0"


def _sig(b): return "sha256=" + hmac.new(SECRET.encode(), b, hashlib.sha256).hexdigest()
def _body(status="FINISHED", agent_id="bc_cycle_agent", ts=None):
    p = {"event": "statusChange", "status": status, "id": agent_id}
    if ts: p["timestamp"] = ts
    return json.dumps(p).encode()


def _seed_agents(db, owner):
    from app.workforce.first_team import bootstrap_first_team
    from app.workforce.first_delegation_scenario import _seed_research_candidates
    bootstrap_first_team(db, owner_id=owner); _seed_research_candidates(db, owner)


def _cycle(db, owner, *, capability="public_text_classify", with_provider=True, absence_enabled=False):
    from app.agent_coordination.autonomous_cycle import run_authorized_operating_cycle
    from app.agent_coordination.durable import DurableCoordinationService
    svc = DurableCoordinationService()
    kw = dict(owner_id=str(owner), goal_text="first-cycle acceptance", required_capability=capability,
              absence_enabled=absence_enabled, standing_grants=None)
    if with_provider:
        b = _body(ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        kw.update(provider_secret=SECRET, provider_raw_body=b, provider_signature=_sig(b), provider_user_agent=UA)
    return svc, run_authorized_operating_cycle(db, svc, **kw)


def _workforce_assignment_count(db, owner):
    from app.models.workforce import WorkforceAssignment
    return db.query(WorkforceAssignment).filter_by(owner_id=owner).count()


# ---------------------------------------------------------------------------
# REQUIRED end-state — currently UNMET on #314 (strict xfail -> flips on a corrected successor)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="ACCEPT-1: a real workforce-selected agent must yield a "
                   "BROKER-ISSUED assignment to a valid coordination recipient. On #314 the selected "
                   "agent_key is rejected by service.assign (SELECTOR-ASSIGN-NAMESPACE) and the broker "
                   "is never used (DELEGATE-BROKER-BYPASS).")
def test_ACCEPT1_selector_to_valid_recipient_to_broker_assignment(db_session, owner_id):
    _seed_agents(db_session, owner_id)
    before = _workforce_assignment_count(db_session, owner_id)
    svc, r = _cycle(db_session, owner_id, capability="public_text_classify")
    # required: the cycle delegates the evidenced agent through the workforce broker and completes
    assert r.get("outcome") == "cycled", r
    assert r.get("selector", {}).get("note") == "workforce_selector", r.get("selector")
    assert _workforce_assignment_count(db_session, owner_id) == before + 1, "no broker-issued WorkforceAssignment"


def test_ACCEPT2_capability_default_fallback_is_transparently_labeled(db_session, owner_id):
    """When NO workforce agent exists for the capability, the cycle may use a coordination-recipient
    default, but it must LABEL that honestly (never present the default as a real workforce selection).
    This holds on #314 (note='no_workforce_agent_use_capability_default') and is a regression anchor;
    the genuine defect — an EXISTING agent that cannot be delegated — is ACCEPT-1."""
    svc, r = _cycle(db_session, owner_id, capability="public_text_classify")  # no agents seeded
    assert r.get("outcome") == "cycled"
    sel = r.get("selector") or {}
    assert sel.get("note") == "no_workforce_agent_use_capability_default"   # fallback is explicit, not masked
    assert sel.get("note") != "workforce_selector", "a hardcoded default must not claim workforce selection"


# ---------------------------------------------------------------------------
# Already-met legs — must stay PASS on any candidate (regression anchors; not duplicating fc314)
# ---------------------------------------------------------------------------

def test_ACCEPT3_no_simulated_event_reported_as_genuine_delivery(db_session, owner_id):
    svc, r = _cycle(db_session, owner_id)
    assert r.get("outcome") == "cycled"
    assert r.get("vendor_delivery_claimed") is False and r.get("live_paid_mutate") is False
    assert r.get("independently_verified") is False     # provider-reported != independently verified


def test_ACCEPT4_founder_available_during_background(db_session, owner_id):
    svc, r = _cycle(db_session, owner_id)
    inter = r.get("interactive") or {}
    assert (r.get("founder") or {}).get("admitted") is True
    assert inter.get("background_cannot_monopolize_interactive", True) is True


def test_ACCEPT5_stop_halts_cycle(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch
    activate_kill_switch(db_session, owner_id=owner_id, reason="stop")
    svc, r = _cycle(db_session, owner_id, absence_enabled=True)
    assert r.get("outcome") != "cycled"
    assert (r.get("absence") or {}).get("stop_blocked") is True


# ---------------------------------------------------------------------------
# BLOCKED — needs the corrected broker wiring to evaluate end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="BLOCKED pending the corrected successor: durable recovery without DUPLICATE "
                         "BROKER-ISSUED assignments and without duplicate external effects can only be "
                         "evaluated once the cycle actually issues broker assignments (ACCEPT-1). On #314 "
                         "no WorkforceAssignment is created by a tick, so there is nothing to recover.")
def test_ACCEPT6_durable_recovery_no_duplicate_broker_assignment():  # pragma: no cover
    ...


@pytest.mark.skip(reason="BLOCKED pending the corrected successor + Founder live authority: spend "
                         "reservation inside the live delegation leg (SPEND-STOP is closed at the spend "
                         "layer; the cycle does not yet reserve spend because it never reaches a paid "
                         "provider call). Re-enable when the corrected cycle performs an authorized "
                         "provider reservation.")
def test_ACCEPT7_spend_reserved_within_grant_during_delegation():  # pragma: no cover
    ...
