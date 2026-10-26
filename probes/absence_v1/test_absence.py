"""#310 operational-handover adversarial acceptance (examiner-owned).

Subject: #310 beecf0e3 (parallel to #309; branched from #306 2444bcc). Exercises the REAL
app.mainai_operational_handover policy/model functions (absence / interactive / self_improve /
gates) + DurableCoordinationService persistence.

Determination recorded by these tests: #310 ships deterministic, fail-closed POLICY/MODEL
functions with durable state — NOT a wired autonomous execution loop. "MainAI stays available
while background work continues" is demonstrable at the admission-control MODEL level; live
wiring to the request/execution path is NOT_IMPLEMENTED (no autonomous GO; first_cycle_go=false).

PASS = the function really enforces this. xfail(strict) = required behavior not met.
No DB, no network, no paid calls. first_cycle_go=false.
"""
from __future__ import annotations

import pytest

from _guards import egress  # noqa: F401

OWNER = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# A. Absence policy — STOP, silence, high-risk, retries, standing authorization
# ---------------------------------------------------------------------------

def test_AB1_stop_pauses_everything():
    from app.mainai_operational_handover.absence import evaluate_absence_tick
    r = evaluate_absence_tick(items=[{"id": "t1", "risk": "read_only_audit", "approved": True}],
                              stop_blocked=True, absence_enabled=True)
    assert r["continued"] == [] and r["paused"] and r["paused"][0]["reason"] == "STOP"
    assert r["silence_is_not_approval"] is True and r["uncontrolled_spend"] is False


def test_AB2_silence_is_never_approval():
    from app.mainai_operational_handover.absence import evaluate_absence_tick
    r = evaluate_absence_tick(items=[{"id": "a1", "needs_approval": True, "approved": False,
                                      "risk": "read_only_audit"}], stop_blocked=False, absence_enabled=True)
    assert not r["continued"]
    assert r["paused"][0]["pause_reason"] == "approval_required"
    assert r["paused"][0]["silence_is_not_approval"] is True


def test_AB3_high_risk_pauses_while_low_risk_continues():
    from app.mainai_operational_handover.absence import evaluate_absence_tick
    r = evaluate_absence_tick(items=[
        {"id": "hi", "risk": "deploy", "approved": True},
        {"id": "lo", "risk": "read_only_audit", "approved": True},
    ], stop_blocked=False, absence_enabled=True)
    cont = {i["id"] for i in r["continued"]}
    paused = {i["id"] for i in r["paused"]}
    assert "lo" in cont and "hi" in paused and r["other_eligible_continues"] is True


def test_AB4_recoverable_retries_bounded():
    from app.mainai_operational_handover.absence import evaluate_absence_tick, MAX_RECOVERABLE_RETRIES
    r = evaluate_absence_tick(items=[{"id": "r1", "risk": "index", "approved": True,
                                      "recoverable_failure": True, "retries": MAX_RECOVERABLE_RETRIES}],
                              stop_blocked=False, absence_enabled=True)
    assert r["paused"] and r["paused"][0]["pause_reason"] == "retry_budget_exhausted"
    assert r["indefinite_retry"] is False


def test_AB5_new_work_needs_standing_grant_unless_low_risk():
    from app.mainai_operational_handover.absence import evaluate_absence_tick
    items = [
        {"id": "n_hi", "risk": "integration", "approved": True, "new_work": True, "scope": "deploys"},
        {"id": "n_lo", "risk": "read_only_audit", "approved": True, "new_work": True, "scope": "audit"},
        {"id": "n_granted", "risk": "reconcile", "approved": True, "new_work": True, "scope": "reconcile"},
    ]
    r = evaluate_absence_tick(items=items, stop_blocked=False, absence_enabled=True,
                              standing_grants=[{"scope": "reconcile", "active": True}])
    deferred = {i["id"] for i in r["deferred_new"]}
    cont = {i["id"] for i in r["continued"]}
    assert "n_hi" in deferred           # high-risk new work w/o grant deferred
    assert "n_lo" in cont               # low-risk new work continues
    assert "n_granted" in cont          # covered by a standing grant


def test_AB6_spend_exhausted_pauses_only_provider_costing():
    from app.mainai_operational_handover.absence import evaluate_absence_tick
    r = evaluate_absence_tick(items=[
        {"id": "pays", "risk": "index", "approved": True, "costs_provider": True},
        {"id": "free", "risk": "index", "approved": True},
    ], stop_blocked=False, spend_exhausted=True, absence_enabled=True)
    assert "pays" in {i["id"] for i in r["paused"]}
    assert "free" in {i["id"] for i in r["continued"]}


def test_AB7_catchup_report_is_honest():
    from app.mainai_operational_handover.absence import evaluate_absence_tick, founder_catchup_report
    ev = evaluate_absence_tick(items=[
        {"id": "done", "title": "indexed corpus", "risk": "index", "approved": True},
        {"id": "wait", "title": "deploy v2", "risk": "deploy", "approved": True},
    ], stop_blocked=False, absence_enabled=True)
    rep = founder_catchup_report(ev)
    assert rep["silence_was_not_treated_as_approval"] is True
    assert any(x["id"] == "wait" for x in rep["paused_needing_founder"])
    assert "deploy v2" in rep["could_not_accomplish"]


# ---------------------------------------------------------------------------
# B. Interactive responsiveness + resource isolation (model)
# ---------------------------------------------------------------------------

def test_IN1_interactive_always_admitted_even_when_background_saturated():
    from app.mainai_operational_handover.interactive import InteractiveIsolation
    iso = InteractiveIsolation(max_background_in_flight=2, max_background_queued=2)
    for n in range(2):
        assert iso.admit("background", name=f"bg{n}")["admitted"] is True
    # background now saturated; interactive must still be admitted immediately
    r = iso.admit("interactive", name="founder_request")
    assert r["admitted"] is True and r["priority"] == 0


def test_IN2_background_bounded_with_backpressure():
    from app.mainai_operational_handover.interactive import InteractiveIsolation
    iso = InteractiveIsolation(max_background_in_flight=1, max_background_queued=1)
    assert iso.admit("background", name="b1")["admitted"] is True   # in flight
    q = iso.admit("background", name="b2")                           # queued
    assert q["admitted"] is False and q.get("queued") is True
    rej = iso.admit("background", name="b3")                         # queue full -> backpressure
    assert rej["admitted"] is False and rej.get("backpressure") is True
    assert iso.public_view()["background_cannot_monopolize_interactive"] is True


def test_IN3_provider_not_permanently_bound_to_category():
    from app.mainai_operational_handover.interactive import InteractiveIsolation
    assert InteractiveIsolation().public_view()["provider_not_permanently_assigned_to_category"] is True


# ---------------------------------------------------------------------------
# C. Bounded self-improvement — proposals never grant authority or deploy
# ---------------------------------------------------------------------------

def _svc():
    from app.agent_coordination.durable import DurableCoordinationService
    return DurableCoordinationService()


def test_SI1_untrusted_actor_rejected():
    from app.mainai_operational_handover.self_improve import run_bounded_self_improvement_cycle
    r = run_bounded_self_improvement_cycle(_svc(), owner_id=OWNER, observed_gaps=[{"description": "x"}],
                                           authorized_scope="s", actor="cursor_provider")
    assert r["accepted"] is False and r["authority_expanded"] is False and r["deployed"] is False


def test_SI2_proposals_are_bounded_and_require_review():
    from app.mainai_operational_handover.self_improve import run_bounded_self_improvement_cycle
    gaps = [
        {"description": "evidence gap", "evidence_backed": True, "capability": "engineering", "priority": 10},
        {"description": "provider marketing idea", "provider_idea": True, "evidence_backed": False},
    ]
    r = run_bounded_self_improvement_cycle(_svc(), owner_id=OWNER, observed_gaps=gaps,
                                           authorized_scope="scope-A", actor="mainai")
    cyc = r["cycle"]
    assert r["accepted"] is True and len(cyc["proposals"]) == 1          # provider-idea w/o evidence skipped
    p = cyc["proposals"][0]
    assert p["requires_independent_review"] is True and p["authority_expanded"] is False and p["deployed"] is False
    assert cyc["stop_bypassed"] is False and cyc["budget_expanded"] is False and cyc["protections_removed"] is False
    assert cyc["delegated_engineering"] is False and cyc["deployed"] is False  # prep != deploy


def test_SI3_unverified_lesson_not_governing_verified_is_preserved():
    from app.mainai_operational_handover.self_improve import run_bounded_self_improvement_cycle, preserve_verified_lesson
    svc = _svc()
    r = run_bounded_self_improvement_cycle(svc, owner_id=OWNER, observed_gaps=[{"description": "g", "evidence_backed": True}],
                                           authorized_scope="s", actor="mainai")
    cid = r["cycle"]["cycle_id"]
    assert preserve_verified_lesson(svc, cycle_id=cid, lesson="L", independently_verified=False)["accepted"] is False
    ok = preserve_verified_lesson(svc, cycle_id=cid, lesson="L", independently_verified=True)
    assert ok["accepted"] is True and ok["lesson"]["authorization_still_valid"] is False


def test_SI4_self_improve_state_is_serialized_for_durability():
    """dump_for() includes the operational_handover block, so self-improve cycles are carried
    into the durable owner ledger (reload path is load_owner_ledger, DB-backed)."""
    from app.mainai_operational_handover.self_improve import run_bounded_self_improvement_cycle
    svc = _svc()
    run_bounded_self_improvement_cycle(svc, owner_id=OWNER, observed_gaps=[{"description": "g", "evidence_backed": True}],
                                       authorized_scope="s", actor="mainai")
    dumped = svc.dump_for("any-assignment")
    ops = dumped.get("operational_handover") or {}
    cycles = (ops.get("self_improve") or {}).get("cycles", [])
    assert len(cycles) >= 1, "self-improve cycle not serialized into the durable dump"


# ---------------------------------------------------------------------------
# D. Honest gate reporting
# ---------------------------------------------------------------------------

def test_GA1_unpassed_gate_never_claimed():
    from app.mainai_operational_handover.gates import gate_status
    g = gate_status(vendor_round_trip=False, five_by_five_gb_measured=False)
    assert g["gate_1_safe_operational_handover"]["passed"] is False      # needs real round trip + trial
    assert g["gate_2_large_archive_production"]["passed"] is False       # needs 5x5GB measured
    assert g["unpassed_gate_never_claimed"] is True
    # Gate-1 still false even if only the round trip is present (trial approval also required)
    assert gate_status(vendor_round_trip=True)["gate_1_safe_operational_handover"]["passed"] is False


# ---------------------------------------------------------------------------
# E. Autonomous delegation / capability-based selection is NOT wired (xfail strict)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="DELEGATE-WIRING: self-improvement names a "
                   "delegate_to_capability but never actually delegates (delegated_engineering=False) "
                   "and no capability-based SELECTOR executes the proposal. Dynamic capability-based "
                   "agent selection is a proposal field, not a wired, executing selector.")
def test_IE1_capability_selection_actually_delegates():
    from app.mainai_operational_handover.self_improve import run_bounded_self_improvement_cycle
    r = run_bounded_self_improvement_cycle(_svc(), owner_id=OWNER,
                                           observed_gaps=[{"description": "g", "evidence_backed": True,
                                                           "capability": "engineering"}],
                                           authorized_scope="s", actor="mainai")
    assert r["cycle"]["delegated_engineering"] is True, "proposal was prepared but never delegated/executed"
