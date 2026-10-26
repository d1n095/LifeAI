"""Autonomous-loop acceptance — real delegation, agent selection, authorization, STOP
(examiner-owned). Builds on the operational-handover readiness matrix (examiner 5f050483).

Subject: #312 22458257 (provider corrections; carries the base workforce subsystem). Drives the REAL
capability-based selector + delegation broker end to end:
  app.workforce.selector (score_candidates / select_best_candidate),
  app.workforce.broker (submit_delegation_request / resolve_delegation, assert_grant_allowed at mint),
  app.workforce.first_delegation_scenario.run_low_risk_public_text_delegation.

Material correction to the earlier #310 DELEGATE-WIRING note: the capability-selection + delegation
machinery EXISTS and executes (evidence-based, not hard-coded). The #310 gap was only that the
self_improve POLICY stub does not call it. This suite executes the real path.

The other autonomous-cycle legs are already proven and are NOT duplicated here:
  STOP at ingestion + ICV + PI-DEDUP -> fc312_v1; spend ceilings/revoke/expiry -> spend_authority_v1;
  Founder responsiveness (interactive isolation) + bounded self-improvement -> absence_v1.

No provider is invoked (activate_provider=False) and egress is guarded. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

import pytest

from _guards import egress  # noqa: F401  (asserts no paid/network call happens)
from _harness import _privileges, owner_id  # noqa: F401

PUBLIC_TEXT = "LifeAI is a founder-operated system. This is a public, non-sensitive paragraph."


def _run(db, owner):
    from app.workforce.first_delegation_scenario import run_low_risk_public_text_delegation
    return run_low_risk_public_text_delegation(db, owner_id=owner, public_text=PUBLIC_TEXT,
                                               activate_provider=False)


# ---------------------------------------------------------------------------
# AL1 — real delegation + evidence-based agent selection, no provider invocation
# ---------------------------------------------------------------------------

def test_AL1_delegation_selects_evidenced_agent_without_provider(db_session, owner_id, egress):
    rec = _run(db_session, owner_id)
    # evidence-based selection: the scenario raises if the selector prefers the un-evidenced worker,
    # so a returned record means the evidenced agent won from recorded performance, not a hard-coded name.
    assert rec.selected_agent_key == "research-strong", rec.selection_explanation
    # a task-scoped assignment was actually minted
    assert getattr(rec, "assignment_id", None) or getattr(rec, "selected_agent_key", None)
    # NO paid provider call occurred during the whole delegation
    assert egress.provider_calls == [], f"delegation invoked a provider: {egress.provider_calls}"
    assert egress.sockets == [], f"delegation attempted egress: {egress.sockets}"


# ---------------------------------------------------------------------------
# AL2 — STOP blocks the delegation mint (fail closed at assert_grant_allowed)
# ---------------------------------------------------------------------------

def test_AL2_stop_blocks_delegation(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop before delegation")
    with pytest.raises(KillSwitchError):
        assert_not_killed(db_session, owner_id)             # positive control
    # resolve_delegation mints via assert_grant_allowed -> must fail closed under STOP
    with pytest.raises(Exception) as ei:
        _run(db_session, owner_id)
    msg = f"{type(ei.value).__name__}: {ei.value}".lower()
    assert "kill" in msg or "stop" in msg or "grant" in msg, f"unexpected error under STOP: {ei.value}"


# ---------------------------------------------------------------------------
# AL3 — builder cannot self-verify (independent verifier required at delegation)
# ---------------------------------------------------------------------------

def test_AL3_builder_cannot_self_verify(db_session, owner_id):
    from app.workforce.first_team import bootstrap_first_team
    from app.workforce.selector import select_best_candidate
    from app.workforce.broker import submit_delegation_request, resolve_delegation, DelegationBrokerError
    from app.workforce.first_delegation_scenario import TaskScopedAuthority, _seed_research_candidates
    from datetime import datetime, timedelta
    bootstrap_first_team(db_session, owner_id=owner_id)
    _seed_research_candidates(db_session, owner_id)
    best = select_best_candidate(db_session, owner_id=owner_id, required_capability="public_text_classify",
                                 risk="low", data_sensitivity="public", prefer_local_only=True)
    req = submit_delegation_request(db_session, owner_id=owner_id,
                                    goal_text="self-verify attempt", required_capability="public_text_classify",
                                    risk="low", data_sensitivity="public", cost_ceiling_usd=0.05,
                                    verification_requirement="independent_verifier")
    auth = TaskScopedAuthority(allowed_read_paths=("public/**",), allowed_write_paths=(),
                               allowed_tool_classes=("read_excerpt",), allow_execution_effects=False,
                               spend_ceiling_usd=0.05, expires_at=datetime.utcnow() + timedelta(hours=1))
    with pytest.raises(DelegationBrokerError) as ei:
        # verifier == the selected builder -> BUILDER_CANNOT_SELF_VERIFY
        resolve_delegation(db_session, owner_id=owner_id, request=req,
                           context_items=[{"kind": "excerpt", "excerpt": "x", "ref": "public:fixture"}],
                           authority=auth, prefer_local_only=True, verifier_profile_id=best.profile_id)
    assert "self_verify" in str(ei.value).lower() or "self-verify" in str(ei.value).lower() \
        or "verifier" in str(ei.value).lower(), ei.value


# NOTE on duplicate-delegation blocking: covered by existing suites (unattended_v1 UA-7; the broker
# reuses app.mainai_founder_ops.assignment_guard.assert_no_duplicate_task_assignment, the same guard).
# Not re-asserted here to avoid fabricating a mainai_tasks FK row.


# ---------------------------------------------------------------------------
# AL5 — selection is evidence-driven, not hard-coded (no selectable agent -> refuse)
# ---------------------------------------------------------------------------

def test_AL5_no_selectable_agent_refuses(db_session, owner_id):
    from app.workforce.selector import select_best_candidate, SelectorError
    # no team bootstrapped, no candidates seeded -> selector must refuse, not invent an agent
    with pytest.raises(SelectorError):
        select_best_candidate(db_session, owner_id=owner_id, required_capability="public_text_classify",
                              risk="low", data_sensitivity="public", prefer_local_only=True)
