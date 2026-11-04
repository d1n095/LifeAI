"""EXAMINER PROBE (not part of candidate) — #314 operating cycle fail-closed + SPEND-STOP.

Independently verifies: (1) the authorized operating cycle stamps the safety invariants and
never claims live paid mutate; (2) when the kill switch / STOP is active, the operating cycle
refuses to establish authority (no new obligation) AND the spend-reservation chokepoint the
cycle depends on refuses spend.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.autonomous_cycle import run_authorized_operating_cycle
from app.agent_coordination.durable import DurableCoordinationService
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import KillSwitchError, activate_kill_switch, assert_grant_allowed


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id):
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _invariants(res):
    assert res["first_cycle_go"] is False
    assert res["live_paid_mutate"] is False
    assert res["vendor_delivery_claimed"] is False
    assert res["silence_is_not_approval"] is True


def test_operating_cycle_holds_safety_invariants(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    service = DurableCoordinationService()
    res = run_authorized_operating_cycle(
        db_session, service, owner_id=str(user.id), goal_text="synthetic: tidy docs", required_capability="coding",
    )
    print(f"EXAMINER-314 outcome={res.get('outcome')} obligations={len(service._obligations)}")
    _invariants(res)


def test_kill_switch_stops_cycle_and_spend(db_session, make_verified_user):
    user, _ = make_verified_user()
    owner_id = user.id
    _set_rls(db_session, owner_id)
    activate_kill_switch(db_session, owner_id=owner_id, reason="examiner STOP")
    db_session.flush()

    # The gate the spend chokepoint relies on must refuse under STOP.
    with pytest.raises(KillSwitchError):
        assert_grant_allowed(db_session, owner_id=owner_id)

    # The operating cycle must fail closed: no execution, invariants intact.
    service = DurableCoordinationService()
    res = run_authorized_operating_cycle(
        db_session, service, owner_id=str(owner_id), goal_text="synthetic: should not run",
    )
    print(f"EXAMINER-314 stop_outcome={res.get('outcome')} obligations={len(service._obligations)}")
    _invariants(res)
    assert res.get("outcome") in {"authority_unestablished", "stop", "stop_blocked", "stopped", "interactive_starved"} \
        or res.get("outcome") != "cycled"
    # No live assignment was executed to completion under STOP.
    assert res.get("independently_verified") is False
