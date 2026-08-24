"""Founder-only production API (app/routers/execution_envelopes.py) making the
proposed-scope -> authorized-envelope edge reachable. Real local Postgres (RLS included),
through the real FastAPI TestClient -- not calling authorize_execution_scope() directly. This
is the file that proves RUNTIME REACHABLE for this specific edge, matching tests/backend/
test_project_entities_api.py's own established discipline for the layer directly below it."""

import uuid

from sqlalchemy import text as sa_text

from app.execution_envelopes import propose_execution_scope
from app.founder import FOUNDER_USER_ID
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


def _founder_goal_and_proposal(db_session, proposed_capabilities=None):
    _set_rls_user(db_session, FOUNDER_USER_ID)
    goal = create_goal(db_session, owner_id=FOUNDER_USER_ID, title="Envelope API test goal", original_instruction="Ship it.", created_by="test")
    db_session.commit()
    _set_rls_user(db_session, FOUNDER_USER_ID)
    proposal = propose_execution_scope(
        db_session, owner_id=FOUNDER_USER_ID, goal_id=goal.id, idempotency_key=f"api-test-{uuid.uuid4()}",
        proposed_paths=["backend/app/foo.py"], proposed_capabilities=proposed_capabilities or ["repo_read", "repo_edit"], proposed_risk="low",
    )
    db_session.commit()
    return goal, proposal


# ---------------------------------------------------------------- A. auth


def test_every_endpoint_requires_authentication(client):
    proposal_id = str(uuid.uuid4())
    envelope_id = str(uuid.uuid4())
    goal_id = str(uuid.uuid4())
    assert client.get("/api/execution-envelopes/proposals").status_code in (401, 403)
    assert client.get(f"/api/execution-envelopes/proposals/{proposal_id}").status_code in (401, 403)
    assert client.post(f"/api/execution-envelopes/proposals/{proposal_id}/authorize", json={"authorized_paths": [], "authorized_capabilities": [], "authorized_risk": "low"}).status_code in (401, 403)
    assert client.post(f"/api/execution-envelopes/proposals/{proposal_id}/reject", json={"reason": "x"}).status_code in (401, 403)
    assert client.get(f"/api/execution-envelopes/current?goal_id={goal_id}").status_code in (401, 403)
    assert client.get(f"/api/execution-envelopes/history?goal_id={goal_id}").status_code in (401, 403)
    assert client.get(f"/api/execution-envelopes/{envelope_id}").status_code in (401, 403)


def test_an_ordinary_member_is_denied(client, make_verified_user):
    user, password = make_verified_user(role="member")
    csrf = _login(client, email=user.email, password=password)
    res = client.get("/api/execution-envelopes/proposals", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 403


def test_a_founder_role_user_with_the_wrong_fixed_identity_is_denied(client, make_verified_user):
    user, password = make_verified_user(role="founder")
    assert user.id != FOUNDER_USER_ID
    csrf = _login(client, email=user.email, password=password)
    res = client.get("/api/execution-envelopes/proposals", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 403


def test_the_actual_founder_succeeds(client):
    csrf = _login(client)
    res = client.get("/api/execution-envelopes/proposals", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200


# ---------------------------------------------------------------- B. authority cannot be spoofed


def test_authorized_by_cannot_be_set_by_the_client(client, db_session):
    _, proposal = _founder_goal_and_proposal(db_session)
    csrf = _login(client)

    res = client.post(
        f"/api/execution-envelopes/proposals/{proposal.id}/authorize",
        json={"authorized_paths": [], "authorized_capabilities": [], "authorized_risk": "low", "authorized_by": "not-actually-the-founder"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    assert res.json()["authorized_by"] == "founder"  # ignored the client-submitted value entirely


def test_owner_id_in_the_request_body_is_ignored(client, db_session):
    _, proposal = _founder_goal_and_proposal(db_session)
    csrf = _login(client)

    other_owner = str(uuid.uuid4())
    res = client.post(
        f"/api/execution-envelopes/proposals/{proposal.id}/authorize",
        json={"authorized_paths": [], "authorized_capabilities": [], "authorized_risk": "low", "owner_id": other_owner},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    envelope_id = res.json()["id"]
    # Reading it back through the SAME authenticated founder session confirms it belongs to
    # the founder, not other_owner -- RLS would hide it from this session otherwise.
    fetched = client.get(f"/api/execution-envelopes/{envelope_id}", headers={"X-CSRF-Token": csrf})
    assert fetched.status_code == 200


# ---------------------------------------------------------------- C. fails closed


def test_authorizing_a_nonexistent_proposal_fails_closed(client):
    csrf = _login(client)
    res = client.post(
        f"/api/execution-envelopes/proposals/{uuid.uuid4()}/authorize",
        json={"authorized_paths": [], "authorized_capabilities": [], "authorized_risk": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 400


def test_getting_a_nonexistent_envelope_returns_404(client):
    csrf = _login(client)
    res = client.get(f"/api/execution-envelopes/{uuid.uuid4()}", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 404


def test_current_envelope_is_none_when_never_authorized(client, db_session):
    goal, _ = _founder_goal_and_proposal(db_session)
    csrf = _login(client)
    res = client.get(f"/api/execution-envelopes/current?goal_id={goal.id}", headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    assert res.json() is None


# ---------------------------------------------------------------- D. the governed edge itself


def test_a_proposal_cannot_become_an_envelope_except_through_the_authorize_route(client):
    from app.main import app

    authorizing_routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/execution-envelopes/") and "POST" in getattr(r, "methods", set()) and "authorize" in r.path]
    assert len(authorizing_routes) == 1


def test_founder_can_narrow_the_proposed_scope_through_the_api(client, db_session):
    _, proposal = _founder_goal_and_proposal(db_session, proposed_capabilities=["repo_read", "repo_edit", "run_tests"])
    csrf = _login(client)

    res = client.post(
        f"/api/execution-envelopes/proposals/{proposal.id}/authorize",
        json={"authorized_paths": [], "authorized_capabilities": ["repo_read"], "authorized_risk": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    assert res.json()["authorized_capabilities"] == ["repo_read"]


def test_successful_authorization_produces_the_expected_envelope_and_becomes_current(client, db_session):
    goal, proposal = _founder_goal_and_proposal(db_session)
    csrf = _login(client)

    res = client.post(
        f"/api/execution-envelopes/proposals/{proposal.id}/authorize",
        json={"authorized_paths": ["backend/app/foo.py"], "authorized_capabilities": ["repo_read", "repo_edit"], "authorized_risk": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    envelope = res.json()
    assert envelope["status"] == "active"
    assert envelope["goal_id"] == str(goal.id)

    current_res = client.get(f"/api/execution-envelopes/current?goal_id={goal.id}", headers={"X-CSRF-Token": csrf})
    assert current_res.status_code == 200
    assert current_res.json()["id"] == envelope["id"]

    updated_proposal_res = client.get(f"/api/execution-envelopes/proposals/{proposal.id}", headers={"X-CSRF-Token": csrf})
    assert updated_proposal_res.json()["status"] == "authorized"
    assert updated_proposal_res.json()["authorized_envelope_id"] == envelope["id"]


def test_reject_path_is_durable_never_deletes(client, db_session):
    _, proposal = _founder_goal_and_proposal(db_session)
    csrf = _login(client)

    res = client.post(
        f"/api/execution-envelopes/proposals/{proposal.id}/reject",
        json={"reason": "This goal should never have autonomous repository access."},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "rejected"

    refetched = client.get(f"/api/execution-envelopes/proposals/{proposal.id}", headers={"X-CSRF-Token": csrf})
    assert refetched.status_code == 200
    assert "autonomous repository access" in refetched.json()["rejected_reason"]


def test_cannot_authorize_an_already_rejected_proposal(client, db_session):
    _, proposal = _founder_goal_and_proposal(db_session)
    csrf = _login(client)

    client.post(f"/api/execution-envelopes/proposals/{proposal.id}/reject", json={"reason": "no"}, headers={"X-CSRF-Token": csrf})
    res = client.post(
        f"/api/execution-envelopes/proposals/{proposal.id}/authorize",
        json={"authorized_paths": [], "authorized_capabilities": [], "authorized_risk": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 400


# ---------------------------------------------------------------- E. real route-level composed proof


def test_real_claim_to_authorized_execution_envelope_through_both_founder_apis_end_to_end(client, db_session):
    """PRODUCTION E2E for the FULL closing-phase chain now available end to end: a real claim
    -> interpretation proposal -> app/routers/project_entities.py's founder API promotes it to
    a ProjectEntity -> the SAME promotion auto-proposes a work candidate -> the SAME founder
    API authorizes it into a real MainAIGoal (which itself auto-proposes an execution scope)
    -> app/routers/execution_envelopes.py's founder API authorizes THAT into a real,
    active ExecutionAuthorizationEnvelope. Every governed step goes through a real HTTP
    request with real founder authentication; only claim/proposal creation (never a manual
    API action in production either) does not."""

    from app.models.document import ActiveTruthStatus, Document, DocumentSource
    from app.models.knowledge_claim import ClaimType, KnowledgeClaim
    from app.project_entities import record_interpretation_proposal

    _set_rls_user(db_session, FOUNDER_USER_ID)
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=FOUNDER_USER_ID, active_truth_status=ActiveTruthStatus.active)
    db_session.add(document)
    db_session.flush()
    claim = KnowledgeClaim(owner_id=FOUNDER_USER_ID, source_id=document.id, claim_text="Vi bör migrera databasen till Postgres innan Q1.", extraction_version="v1", claim_type=ClaimType.decision)
    db_session.add(claim)
    db_session.commit()
    _set_rls_user(db_session, FOUNDER_USER_ID)
    proposal = record_interpretation_proposal(db_session, owner_id=FOUNDER_USER_ID, source_claim_id=claim.id, proposed_entity_type="decision", idempotency_key="e2e-envelope-prop")
    db_session.commit()

    csrf = _login(client)

    promote_res = client.post(
        f"/api/project-entities/interpretation-proposals/{proposal.id}/promote",
        json={"entity_type": "decision", "title": "Migrera databasen till Postgres innan Q1."},
        headers={"X-CSRF-Token": csrf},
    )
    assert promote_res.status_code == 201, promote_res.text
    entity_id = promote_res.json()["id"]

    candidates_res = client.get("/api/project-entities/work-candidates?status_filter=unreviewed", headers={"X-CSRF-Token": csrf})
    candidate = next(c for c in candidates_res.json() if c["source_entity_id"] == entity_id)

    authorize_goal_res = client.post(
        f"/api/project-entities/work-candidates/{candidate['id']}/authorize",
        json={"risk_level": "low", "approval_policy": "standard_repo_work"},
        headers={"X-CSRF-Token": csrf},
    )
    assert authorize_goal_res.status_code == 201, authorize_goal_res.text
    goal_id = authorize_goal_res.json()["id"]

    scope_proposals_res = client.get(f"/api/execution-envelopes/proposals?status_filter=unreviewed&goal_id={goal_id}", headers={"X-CSRF-Token": csrf})
    assert scope_proposals_res.status_code == 200
    scope_proposals = scope_proposals_res.json()
    assert len(scope_proposals) == 1
    scope_proposal = scope_proposals[0]
    assert scope_proposal["proposed_capabilities"] == ["repo_read", "repo_edit", "run_tests"]

    authorize_envelope_res = client.post(
        f"/api/execution-envelopes/proposals/{scope_proposal['id']}/authorize",
        json={"authorized_paths": ["backend/app/db.py"], "authorized_capabilities": ["repo_read", "repo_edit"], "authorized_risk": "low"},
        headers={"X-CSRF-Token": csrf},
    )
    assert authorize_envelope_res.status_code == 201, authorize_envelope_res.text
    envelope = authorize_envelope_res.json()
    assert envelope["status"] == "active"
    assert envelope["authorized_by"] == "founder"
    assert envelope["authorized_capabilities"] == ["repo_read", "repo_edit"]  # narrowed from the 3-capability proposal

    current_res = client.get(f"/api/execution-envelopes/current?goal_id={goal_id}", headers={"X-CSRF-Token": csrf})
    assert current_res.json()["id"] == envelope["id"]
