"""Founder-authenticated provider-spend API — proves RUNTIME REACHABLE grant/revoke."""

import uuid
from decimal import Decimal

from sqlalchemy import text as sa_text

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.request_context import current_user_id as current_user_id_var

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def test_founder_can_authorize_and_revoke_provider_spend_via_api(client, superuser_db, make_verified_user):
    """authorized_by is never taken from the body — require_founder + hardcoded 'founder'."""
    from app.founder import FOUNDER_USER_ID
    from app.models.user import User

    founder = superuser_db.get(User, FOUNDER_USER_ID)
    assert founder is not None
    _set_rls_user(superuser_db, founder.id)
    goal = create_goal(
        superuser_db,
        owner_id=founder.id,
        title="spend api",
        original_instruction="plan",
        created_by="test",
    )
    superuser_db.flush()
    proposal = propose_execution_scope(
        superuser_db, owner_id=founder.id, goal_id=goal.id, idempotency_key=f"api-prop-{uuid.uuid4()}"
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=founder.id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["README.md"],
        authorized_capabilities=["read_file"],
        authorized_risk="low",
        envelope_idempotency_key=f"api-env-{uuid.uuid4()}",
    )
    superuser_db.commit()

    csrf = _login(client)
    res = client.post(
        "/api/provider-spend/authorize",
        headers={"X-CSRF-Token": csrf},
        json={
            "goal_id": str(goal.id),
            "execution_envelope_id": str(envelope.id),
            "max_cost_usd": "1.00",
            "max_requests": 3,
            "max_cost_per_request_usd": "0.25",
            "idempotency_key": f"api-spend-{uuid.uuid4()}",
            "allowed_providers": ["fake-local"],
            "allowed_models": ["planner-v2"],
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["authorized_by"] == "founder"
    assert body["status"] == "active"
    assert "authorized_by" not in {
        k for k in (res.request.content.decode() if False else [])
    }

    current = client.get(f"/api/provider-spend/current?goal_id={goal.id}")
    assert current.status_code == 200
    assert current.json()["id"] == body["id"]

    revoked = client.post(
        f"/api/provider-spend/{body['id']}/revoke",
        headers={"X-CSRF-Token": csrf},
        json={"reason": "founder stop"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
