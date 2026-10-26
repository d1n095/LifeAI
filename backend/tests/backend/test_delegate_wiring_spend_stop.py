"""DELEGATE-WIRING + SPEND-STOP + absence on existing production services."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text as sa_text

from app.agent_coordination.autonomous_cycle import run_authorized_operating_cycle
from app.agent_coordination.durable import DurableCoordinationService, load_owner_ledger
from app.agent_coordination.external_connectivity.cursor_cloud import WEBHOOK_USER_AGENT
from app.agent_coordination.persist import persist_owner_ledger
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.mainai_operational_handover import evaluate_absence_tick, founder_catchup_report
from app.models.provider_spend import ProviderSpendUsageEvent
from app.models.user import User
from app.provider_spend import ProviderSpendError, authorize_provider_spend, reserve_provider_spend_call
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import activate_kill_switch

SECRET = "fixture-webhook-secret"
AGENT = "bc_cycle_agent"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _sign(payload: dict) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return raw, "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


def test_cycle_refuses_when_authority_unestablished():
    owner = str(uuid.uuid4())
    result = run_authorized_operating_cycle(
        None,
        DurableCoordinationService(),
        owner_id=owner,
        goal_text="authorized work",
    )
    assert result["outcome"] == "authority_unestablished"
    assert result["first_cycle_go"] is False
    assert result["founder"]["admitted"] is True


def test_cycle_wires_ingest_without_independent_certification(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    raw, signature = _sign(
        {
            "event": "statusChange",
            "id": AGENT,
            "status": "FINISHED",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
    )
    result = run_authorized_operating_cycle(
        db_session,
        DurableCoordinationService(),
        owner_id=str(user.id),
        goal_text="compose provider path",
        provider_secret=SECRET,
        provider_raw_body=raw,
        provider_signature=signature,
        provider_user_agent=WEBHOOK_USER_AGENT,
        candidate_sha="b" * 40,
    )
    assert result["outcome"] == "cycled"
    assert result["ingest"]["transition"] == "completed"
    assert result["ingest"]["independently_verified"] is False
    assert result["verification"]["outcome"] == "verification_required"
    assert result["independently_verified"] is False
    assert result["founder"]["admitted"] is True
    assert result["interactive"]["background_cannot_monopolize_interactive"] is True
    assert result["next_eligible"] is not None


def test_cycle_stop_blocks_and_absence_pauses_all(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    activate_kill_switch(db_session, owner_id=user.id, reason="delegate-wiring")
    db_session.commit()
    _set_rls(db_session, user.id)
    result = run_authorized_operating_cycle(
        db_session,
        DurableCoordinationService(),
        owner_id=str(user.id),
        goal_text="should not assign",
        absence_enabled=True,
    )
    assert result["outcome"] == "stop_rejected"
    assert result["absence"]["continued"] == []
    assert result["catchup"]["silence_was_not_treated_as_approval"] is True
    assert result["first_cycle_go"] is False


def test_absence_silence_is_not_permission_and_catchup_is_real():
    evaluation = evaluate_absence_tick(
        items=[
            {"id": "ready", "title": "index", "approved": True, "risk": "index"},
            {"id": "new", "title": "new spend", "new_work": True, "approved": False, "needs_approval": True, "risk": "spend"},
        ],
        stop_blocked=False,
        standing_grants=[{"scope": "index", "active": True}],
        absence_enabled=True,
    )
    assert any(row["id"] == "ready" for row in evaluation["continued"])
    assert any(row.get("pause_reason") == "approval_required" for row in evaluation["paused"])
    assert evaluation["silence_is_not_approval"] is True
    catchup = founder_catchup_report(evaluation)
    assert "new spend" in catchup["could_not_accomplish"]


def test_cycle_absence_and_handover_survive_restart(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls(db_session, user.id)
    service = DurableCoordinationService()
    result = run_authorized_operating_cycle(
        db_session,
        service,
        owner_id=str(user.id),
        goal_text="persist cycle",
        absence_enabled=True,
    )
    assert result["outcome"] == "cycled"
    persist_owner_ledger(db_session, owner_id=user.id, service=service, actor="cycle")
    db_session.commit()
    db_session.expire_all()
    _set_rls(db_session, user.id)
    restored = load_owner_ledger(db_session, owner_id=user.id)
    assert restored._operational_handover.get("absence")
    handover = restored.founder_view()["operational_handover"]
    assert handover["silence_is_not_approval"] is True
    assert handover.get("absence")


def _owner_goal_envelope(db):
    user = User(email=f"spend-stop-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = create_goal(
        db,
        owner_id=user.id,
        title="spend stop probe",
        original_instruction="plan a local edit",
        created_by="test",
    )
    db.flush()
    proposal = propose_execution_scope(
        db, owner_id=user.id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        db,
        owner_id=user.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    db.flush()
    return user, goal, envelope


def test_spend_reservation_fails_closed_during_stop(superuser_db):
    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=3,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()
    activate_kill_switch(superuser_db, owner_id=owner.id, reason="spend-stop")
    superuser_db.commit()
    with pytest.raises(ProviderSpendError, match="STOP"):
        reserve_provider_spend_call(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref=f"ref-{uuid.uuid4()}",
            provider="fake-local",
            model="planner-v2",
        )
    rows = list(
        superuser_db.execute(
            select(ProviderSpendUsageEvent).where(ProviderSpendUsageEvent.owner_id == owner.id)
        ).scalars()
    )
    assert rows == []


def test_spend_reservation_still_fails_when_grant_revoked(superuser_db):
    from app.provider_spend import revoke_provider_spend, get_current_provider_spend_authorization

    owner, goal, envelope = _owner_goal_envelope(superuser_db)
    grant = authorize_provider_spend(
        superuser_db,
        owner_id=owner.id,
        goal_id=goal.id,
        execution_envelope_id=envelope.id,
        authorized_by="founder",
        max_cost_usd=Decimal("1.00"),
        max_requests=2,
        max_cost_per_request_usd=Decimal("0.50"),
        idempotency_key=f"spend-{uuid.uuid4()}",
        allowed_providers=["fake-local"],
        allowed_models=["planner-v2"],
    )
    superuser_db.commit()
    revoke_provider_spend(superuser_db, owner_id=owner.id, authorization_id=grant.id, reason="revoked")
    superuser_db.commit()
    assert get_current_provider_spend_authorization(superuser_db, owner_id=owner.id, goal_id=goal.id) is None
    with pytest.raises(ProviderSpendError):
        reserve_provider_spend_call(
            superuser_db,
            owner_id=owner.id,
            goal_id=goal.id,
            source_ref=f"ref-{uuid.uuid4()}",
            provider="fake-local",
            model="planner-v2",
        )
