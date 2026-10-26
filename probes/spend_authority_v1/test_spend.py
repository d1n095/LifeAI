"""Spend-authority boundary acceptance — external-provider execution/retry/follow-up/continuation
cannot exceed a valid Founder spending grant (examiner-owned).

Subject: #309 aff5de1a. Drives the REAL app.provider_spend.service reservation ledger
(authorize / reserve / settle / release / revoke) + execution-envelope + goal setup, exactly as the
product's own tests build it. Negative tests are executable; the STOP cross-cut that the spend layer
does not yet enforce is an xfail(strict) finding.

Classification:
- PASS  = the spend ledger really enforces this today (executable negative control).
- xfail(strict) = required defense-in-depth the spend layer does not yet do (fails now; XPASS if fixed).
Codex's Security Kernel is NOT modified. first_cycle_go=false. No live/paid provider calls (egress guard).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from _guards import egress  # noqa: F401
from _harness import _privileges, owner_id  # noqa: F401  (autouse privileges + real owner)

FAKE_P, FAKE_M = "fake", "fake-plan-v1"


def _live_grant(db, owner, *, max_cost="1.00", max_requests=5, per_req="0.25",
                providers=(FAKE_P,), models=(FAKE_M,), expires_at=None):
    """goal -> execution envelope -> provider-spend grant, returning (goal_id, envelope_id, grant)."""
    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
    from app.mainai_execution.planner import create_goal
    from app.provider_spend import authorize_provider_spend
    goal = create_goal(db, owner_id=owner, title="spend probe", original_instruction="i", created_by="test")
    db.flush()
    prop = propose_execution_scope(db, owner_id=owner, goal_id=goal.id,
                                   idempotency_key=f"sp-prop-{uuid.uuid4()}")
    _, env = authorize_execution_scope(db, owner_id=owner, proposal_id=prop.id, authorized_by="founder",
                                       authorized_paths=["README.md"], authorized_capabilities=["read_file"],
                                       authorized_risk="low", envelope_idempotency_key=f"sp-env-{uuid.uuid4()}")
    grant = authorize_provider_spend(
        db, owner_id=owner, goal_id=goal.id, execution_envelope_id=env.id, authorized_by="founder",
        max_cost_usd=Decimal(max_cost), max_requests=max_requests,
        max_cost_per_request_usd=Decimal(per_req), idempotency_key=f"sp-grant-{uuid.uuid4()}",
        allowed_providers=list(providers), allowed_models=list(models), expires_at=expires_at)
    db.flush()
    return goal.id, env.id, grant


def _reserve(db, owner, goal_id, *, ref=None, provider=FAKE_P, model=FAKE_M):
    from app.provider_spend import reserve_provider_spend_call
    return reserve_provider_spend_call(db, owner_id=owner, goal_id=goal_id,
                                       source_ref=ref or f"ref-{uuid.uuid4()}", provider=provider, model=model)


# ---------------------------------------------------------------------------
# A. Grant creation validation (PASS; no DB write needed)
# ---------------------------------------------------------------------------

def test_SP_A1_negative_ceilings_rejected(db_session, owner_id):
    from app.provider_spend import authorize_provider_spend, ProviderSpendError
    with pytest.raises(ProviderSpendError):
        authorize_provider_spend(db_session, owner_id=owner_id, goal_id=uuid.uuid4(),
                                 execution_envelope_id=uuid.uuid4(), authorized_by="founder",
                                 max_cost_usd=Decimal("-1"), max_requests=5, idempotency_key="x")
    with pytest.raises(ProviderSpendError):
        authorize_provider_spend(db_session, owner_id=owner_id, goal_id=uuid.uuid4(),
                                 execution_envelope_id=uuid.uuid4(), authorized_by="founder",
                                 max_cost_usd=Decimal("1"), max_requests=-5, idempotency_key="x")


# ---------------------------------------------------------------------------
# B. Reservation cannot exceed the grant (PASS — the core boundary)
# ---------------------------------------------------------------------------

def test_SP_B1_no_grant_refuses(db_session, owner_id):
    from app.provider_spend import ProviderSpendError
    # a goal with no spend grant
    from app.mainai_execution.planner import create_goal
    g = create_goal(db_session, owner_id=owner_id, title="no grant", original_instruction="i", created_by="t")
    db_session.flush()
    with pytest.raises(ProviderSpendError):
        _reserve(db_session, owner_id, g.id)


def test_SP_B2_request_ceiling_enforced(db_session, owner_id):
    from app.provider_spend import ProviderSpendError
    gid, _, _ = _live_grant(db_session, owner_id, max_requests=2, per_req="0.01", max_cost="100")
    _reserve(db_session, owner_id, gid)
    _reserve(db_session, owner_id, gid)
    with pytest.raises(ProviderSpendError):   # 3rd exceeds max_requests=2
        _reserve(db_session, owner_id, gid)


def test_SP_B3_cost_ceiling_enforced(db_session, owner_id):
    from app.provider_spend import ProviderSpendError
    # budget only covers 2 holds of 0.25
    gid, _, _ = _live_grant(db_session, owner_id, max_cost="0.50", max_requests=99, per_req="0.25")
    _reserve(db_session, owner_id, gid)
    _reserve(db_session, owner_id, gid)
    with pytest.raises(ProviderSpendError):
        _reserve(db_session, owner_id, gid)


def test_SP_B4_provider_model_allowlist_enforced(db_session, owner_id):
    from app.provider_spend import ProviderSpendError
    gid, _, _ = _live_grant(db_session, owner_id, providers=(FAKE_P,), models=(FAKE_M,))
    with pytest.raises(ProviderSpendError):
        _reserve(db_session, owner_id, gid, provider="openai")
    with pytest.raises(ProviderSpendError):
        _reserve(db_session, owner_id, gid, model="gpt-4o")


# ---------------------------------------------------------------------------
# C. Revoked / expired grants block continuation (PASS)
# ---------------------------------------------------------------------------

def test_SP_C1_revoked_grant_refuses(db_session, owner_id):
    from app.provider_spend import revoke_provider_spend, ProviderSpendError
    gid, _, grant = _live_grant(db_session, owner_id)
    revoke_provider_spend(db_session, owner_id=owner_id, authorization_id=grant.id, reason="founder revoke")
    with pytest.raises(ProviderSpendError):
        _reserve(db_session, owner_id, gid)


def test_SP_C2_expired_grant_refuses(db_session, owner_id):
    from app.provider_spend import ProviderSpendError
    # product convention is naive UTC (datetime.utcnow throughout); match it.
    past = datetime.utcnow() - timedelta(seconds=1)
    gid, _, _ = _live_grant(db_session, owner_id, expires_at=past)
    with pytest.raises(ProviderSpendError):
        _reserve(db_session, owner_id, gid)


# ---------------------------------------------------------------------------
# D. Duplicate / replay / concurrent reservations do not double-spend (PASS)
# ---------------------------------------------------------------------------

def test_SP_D1_duplicate_source_ref_idempotent(db_session, owner_id):
    gid, _, grant = _live_grant(db_session, owner_id, max_requests=5)
    ref = f"ref-dup-{uuid.uuid4()}"
    e1, c1 = _reserve(db_session, owner_id, gid, ref=ref)
    e2, c2 = _reserve(db_session, owner_id, gid, ref=ref)
    assert c1 is True and c2 is False and e1.id == e2.id
    db_session.refresh(grant)
    assert grant.reserved_requests == 1, "duplicate source_ref double-held budget"


def test_SP_D2_released_source_ref_not_resurrected(db_session, owner_id):
    from app.provider_spend import release_provider_spend_call, ProviderSpendError
    gid, _, _ = _live_grant(db_session, owner_id)
    ref = f"ref-rel-{uuid.uuid4()}"
    _reserve(db_session, owner_id, gid, ref=ref)
    release_provider_spend_call(db_session, owner_id=owner_id, source_ref=ref)
    with pytest.raises(ProviderSpendError):   # append-only: cannot reuse a released ref
        _reserve(db_session, owner_id, gid, ref=ref)


# ---------------------------------------------------------------------------
# E. Forged / tampered approval cannot widen authority (PASS)
# ---------------------------------------------------------------------------

def test_SP_E1_idempotency_reuse_with_different_authority_rejected(db_session, owner_id):
    """The authority fingerprint pins idempotency to the authorized fields: replaying a grant's
    idempotency_key with a HIGHER ceiling is rejected (no silent authority widening)."""
    from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
    from app.mainai_execution.planner import create_goal
    from app.provider_spend import authorize_provider_spend, ProviderSpendError
    goal = create_goal(db_session, owner_id=owner_id, title="fp", original_instruction="i", created_by="t")
    db_session.flush()
    prop = propose_execution_scope(db_session, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"fp-p-{uuid.uuid4()}")
    _, env = authorize_execution_scope(db_session, owner_id=owner_id, proposal_id=prop.id, authorized_by="founder",
                                       authorized_paths=["README.md"], authorized_capabilities=["read_file"],
                                       authorized_risk="low", envelope_idempotency_key=f"fp-e-{uuid.uuid4()}")
    key = f"fp-grant-{uuid.uuid4()}"
    common = dict(owner_id=owner_id, goal_id=goal.id, execution_envelope_id=env.id, authorized_by="founder",
                  idempotency_key=key, allowed_providers=[FAKE_P], allowed_models=[FAKE_M])
    authorize_provider_spend(db_session, max_cost_usd=Decimal("1.00"), max_requests=5, **common)
    with pytest.raises(ProviderSpendError):   # same key, bigger ceiling = tampered authority
        authorize_provider_spend(db_session, max_cost_usd=Decimal("999.00"), max_requests=9999, **common)


# ---------------------------------------------------------------------------
# F. Accurate cost reporting on settlement (PASS)
# ---------------------------------------------------------------------------

def test_SP_F1_settlement_records_actual_cost_and_frees_overhold(db_session, owner_id):
    from app.provider_spend import settle_provider_spend_call
    gid, _, grant = _live_grant(db_session, owner_id, max_cost="1.00", max_requests=5, per_req="0.25")
    ref = f"ref-settle-{uuid.uuid4()}"
    _reserve(db_session, owner_id, gid, ref=ref)
    db_session.refresh(grant)
    assert grant.reserved_cost_usd == Decimal("0.25")
    settle_provider_spend_call(db_session, owner_id=owner_id, source_ref=ref,
                               prompt_tokens=10, completion_tokens=5, cost_usd=Decimal("0.02"))
    db_session.refresh(grant)
    # the hold is released and only the actual settled cost is committed
    assert grant.spent_cost_usd == Decimal("0.02"), grant.spent_cost_usd
    assert grant.reserved_cost_usd == Decimal("0.00"), grant.reserved_cost_usd


# ---------------------------------------------------------------------------
# G. STOP cross-cut — spend layer does not consult the kill switch (xfail strict)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="SPEND-STOP: reserve_provider_spend_call does not consult the "
                   "kill switch, so a reservation can be taken under an active Founder STOP. Contained "
                   "today because provider_worker.assert_not_killed gates EXECUTION upstream, but "
                   "defense-in-depth requires the spend layer to fail closed under STOP too.")
def test_SP_G1_reservation_refused_under_stop(db_session, owner_id):
    from app.workforce.kill_switch import activate_kill_switch, assert_not_killed, KillSwitchError
    from app.provider_spend import ProviderSpendError
    gid, _, _ = _live_grant(db_session, owner_id)
    activate_kill_switch(db_session, owner_id=owner_id, reason="founder stop")
    with pytest.raises(KillSwitchError):     # positive control: STOP genuinely active
        assert_not_killed(db_session, owner_id)
    with pytest.raises(ProviderSpendError):  # REQUIRED: reservation must fail closed under STOP
        _reserve(db_session, owner_id, gid)
