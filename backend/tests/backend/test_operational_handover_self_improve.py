"""Gate-1 operational handover: absence, isolation, self-improve. Gates stay unpassed."""

from __future__ import annotations

import time
import uuid

from app.agent_coordination.durable import DurableCoordinationService, load_owner_ledger
from app.agent_coordination.persist import persist_owner_ledger
from app.mainai_operational_handover import (
    InteractiveIsolation,
    evaluate_absence_tick,
    founder_catchup_report,
    gate_status,
    run_bounded_self_improvement_cycle,
)
from app.mainai_operational_handover.self_improve import preserve_verified_lesson
from app.request_context import current_user_id as current_user_id_var
from sqlalchemy import text as sa_text


def test_absence_continues_approved_work_and_never_treats_silence_as_approval():
    evaluation = evaluate_absence_tick(
        items=[
            {"id": "a", "title": "ready audit", "approved": True, "risk": "read_only_audit"},
            {"id": "b", "title": "needs grant", "needs_approval": True, "approved": False, "new_work": True},
            {"id": "c", "title": "retry", "approved": True, "recoverable_failure": True, "retries": 1, "risk": "index"},
            {"id": "d", "title": "deploy", "approved": True, "risk": "deploy"},
            {
                "id": "e",
                "title": "bypass",
                "approved": True,
                "restriction": {"workaround_class": "prohibited"},
            },
        ],
        stop_blocked=False,
        standing_grants=[{"scope": "read_only_audit", "active": True}],
        absence_enabled=True,
    )
    ids = {row["id"] for row in evaluation["continued"]}
    assert "a" in ids
    assert "c" in ids
    paused = {row["id"]: row["pause_reason"] for row in evaluation["paused"]}
    assert paused["b"] == "approval_required"
    assert paused["d"] == "high_risk_requires_founder"
    assert paused["e"] == "restriction"
    assert evaluation["silence_is_not_approval"] is True
    assert evaluation["indefinite_retry"] is False
    assert evaluation["first_cycle_go"] is False
    catchup = founder_catchup_report(evaluation)
    assert catchup["silence_was_not_treated_as_approval"] is True
    assert "needs grant" in catchup["could_not_accomplish"]


def test_stop_pauses_all_absence_work():
    evaluation = evaluate_absence_tick(
        items=[{"id": "a", "approved": True, "risk": "read_only_audit"}],
        stop_blocked=True,
        absence_enabled=True,
    )
    assert evaluation["continued"] == []
    assert evaluation["paused"][0]["scope"] == "all"


def test_interactive_not_monopolized_by_background_flood():
    iso = InteractiveIsolation(max_background_in_flight=2, max_background_queued=3)
    start = time.monotonic()
    interactive = iso.admit("interactive", name="founder_chat", started_at=start)
    assert interactive["admitted"] is True
    admitted = 0
    rejected_or_queued = 0
    for index in range(12):
        result = iso.admit("background", name=f"import-{index}")
        if result["admitted"]:
            admitted += 1
        else:
            rejected_or_queued += 1
    later = iso.admit("interactive", name="founder_ops")
    assert later["admitted"] is True
    view = iso.public_view()
    assert view["interactive_admitted"] == 2
    assert view["background_rejected"] + view["background_queued"] >= 1
    assert view["background_cannot_monopolize_interactive"] is True
    assert view["provider_not_permanently_assigned_to_category"] is True
    assert admitted <= 2 or view["background_in_flight"] <= 2


def test_self_improve_proposes_without_granting_or_deploying():
    service = DurableCoordinationService()
    result = run_bounded_self_improvement_cycle(
        service,
        owner_id="11111111-1111-1111-1111-111111111111",
        authorized_scope="docs/**",
        observed_gaps=[
            {"description": "missing extraction verification", "capability": "engineering", "priority": 10, "evidence_backed": True},
            {"description": "provider suggested extra budget", "provider_idea": True, "evidence_backed": False},
        ],
    )
    assert result["accepted"] is True
    cycle = result["cycle"]
    assert len(cycle["proposals"]) == 1
    assert cycle["authority_expanded"] is False
    assert cycle["deployed"] is False
    assert cycle["stop_bypassed"] is False
    assert cycle["budget_expanded"] is False
    assert cycle["independently_verified"] is False
    rejected = preserve_verified_lesson(service, cycle_id=cycle["cycle_id"], lesson="do not skip HMAC", independently_verified=False)
    assert rejected["accepted"] is False
    stored = preserve_verified_lesson(service, cycle_id=cycle["cycle_id"], lesson="do not skip HMAC", independently_verified=True)
    assert stored["accepted"] is True
    assert stored["lesson"]["authorization_still_valid"] is False


def test_gates_remain_unpassed_without_vendor_or_5gb_proof():
    status = gate_status()
    assert status["gate_1_safe_operational_handover"]["passed"] is False
    assert status["gate_2_large_archive_production"]["passed"] is False
    assert status["gate_1_safe_operational_handover"]["parts"]["real_external_agent_round_trip"] is False
    assert status["unpassed_gate_never_claimed"] is True
    assert status["first_cycle_go"] is False


def test_self_improve_survives_ledger_restart(db_session, make_verified_user):
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)
    user, _password = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    service = DurableCoordinationService()
    service.assign(
        agent_identity="cursor",
        workstream="self_improve",
        owner_id=str(user.id),
        instruction_id="observe",
        assignment_id=str(uuid.uuid4()),
        actor="founder",
    )
    run_bounded_self_improvement_cycle(
        service,
        owner_id=str(user.id),
        authorized_scope="backend/app/**",
        observed_gaps=[{"description": "gap", "evidence_backed": True, "priority": 1}],
    )
    persist_owner_ledger(db_session, owner_id=user.id, service=service, actor="test")
    db_session.commit()
    db_session.expire_all()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    restored = load_owner_ledger(db_session, owner_id=user.id)
    cycles = restored._operational_handover["self_improve"]["cycles"]
    assert cycles
    assert cycles[0]["deployed"] is False
