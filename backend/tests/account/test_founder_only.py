"""MainAI is a Founder AI, not a shared or per-user assistant (see
docs/FOUNDER_KNOWLEDGE_BOOTSTRAP.md). This covers the specific guarantees requested for the
Founder-only launch: public registration is closed in production, a non-founder account is
denied every MainAI-surface route regardless of how valid its credentials are, the founder
account can use the whole system, and password reset keeps working for it.

Complements (doesn't replace) frontend/e2e/account.spec.ts, which exercises the same
boundary through real HTTP cookies/CSRF/UI rather than FastAPI's TestClient."""

import re

import app.routers.auth as auth_router
from app.founder import FOUNDER_USER_ID
from app.models.user import User, UserRole


def test_register_returns_404_in_production(client, monkeypatch):
    monkeypatch.setattr(auth_router.settings, "environment", "production")
    res = client.post(
        "/api/auth/register", json={"email": "sneaky@example.com", "password": "GoodPassword1234!", "website": ""}
    )
    assert res.status_code == 404


def test_register_still_works_outside_production(client, db_session):
    # Same assertion as test_registration.py's coverage, restated here to make the
    # production/non-production contrast explicit in one place.
    res = client.post(
        "/api/auth/register", json={"email": "dev-only@example.com", "password": "GoodPassword1234!", "website": ""}
    )
    assert res.status_code == 202
    assert db_session.query(User).filter_by(email="dev-only@example.com").first() is not None


def test_direct_register_api_call_blocked_in_production_even_with_a_real_looking_payload(client, monkeypatch, db_session):
    """A "direct API call" isn't functionally different from what the browser sends — the
    same TestClient.post() the UI-driven tests use IS the direct API call. This test exists
    to make that equivalence explicit and to confirm no row is created."""
    monkeypatch.setattr(auth_router.settings, "environment", "production")
    res = client.post(
        "/api/auth/register",
        json={"email": "direct-api-attempt@example.com", "password": "GoodPassword1234!", "website": ""},
    )
    assert res.status_code == 404
    assert db_session.query(User).filter_by(email="direct-api-attempt@example.com").first() is None


def test_non_founder_denied_every_protected_route(client, make_verified_user):
    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]

    for path in ["/api/conversations", "/api/documents", "/api/projects", "/api/admin/providers/status"]:
        res = client.get(path)
        assert res.status_code == 403, f"{path} should 403 for a non-founder, got {res.status_code}"

    chat_res = client.post("/api/chat", json={"message": "hej"}, headers={"X-CSRF-Token": csrf})
    assert chat_res.status_code == 403

    search_res = client.post("/api/knowledge/search", json={"query": "hej"}, headers={"X-CSRF-Token": csrf})
    assert search_res.status_code == 403

    # Generic self-service (not MainAI functionality) stays reachable for the same session —
    # confirms the 403s above are require_founder acting deliberately, not a broken session.
    assert client.get("/api/auth/me").status_code == 200


def test_legacy_admin_role_denied_founder_access(client, make_verified_user):
    """admin/member predate the Founder-only launch (see app/models/user.py's UserRole
    docstring) and are currently unreachable via registration, but nothing in the schema
    prevents a row with role=admin existing (e.g. from before this restriction shipped, or a
    future migration). Confirms such a row is denied exactly like an ordinary member — role
    alone never grants MainAI access, only role AND the fixed founder id together do."""
    user, password = make_verified_user(role="admin")
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    assert login.json()["role"] == "admin"

    for path in ["/api/conversations", "/api/documents", "/api/projects", "/api/admin/providers/status"]:
        res = client.get(path)
        assert res.status_code == 403, f"{path} should 403 for role=admin, got {res.status_code}"


def test_founder_role_without_the_fixed_id_is_still_denied(client, make_verified_user):
    """The exact guarantee app/deps.py's require_founder() docstring claims: "a role check
    alone would trust any row someone managed to mark role=founder" — this proves that
    claim, not just the role half of it. A row with role=founder but a different primary
    key (make_verified_user never sets id, so this gets a random uuid4, never
    FOUNDER_USER_ID) must still be denied. If require_founder() ever regressed to a
    role-only check, this is the test that would catch it — test_non_founder_denied_every_
    protected_route above uses role="member" by default and would NOT catch that specific
    regression."""
    user, password = make_verified_user(role="founder")
    from app.founder import FOUNDER_USER_ID

    assert user.id != FOUNDER_USER_ID  # sanity check on the test's own premise

    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    assert login.json()["role"] == "founder"  # the JWT/session honestly reflects the DB role...

    # ...but require_founder() must still refuse every protected route, because the id
    # doesn't match FOUNDER_USER_ID.
    for path in ["/api/conversations", "/api/documents", "/api/projects", "/api/admin/providers/status"]:
        res = client.get(path)
        assert res.status_code == 403, f"{path} should 403 for a founder-role row with the wrong id, got {res.status_code}"


def test_login_with_unknown_email_returns_the_same_generic_error_as_wrong_password(client):
    """No user-enumeration signal via login: an email that was never registered must fail
    identically (status code and message) to a correct email with a wrong password — see
    app/routers/auth.py's login(), which already collapses `user is None`,
    `not user.is_active`, and a wrong password into one branch. This test exists to lock
    that property in, not to test something currently broken."""
    unknown = client.post(
        "/api/auth/login", json={"email": "never-registered@example.com", "password": "SomePassword123!"}
    )
    wrong_password = client.post(
        "/api/auth/login", json={"email": "founder@lifeos.local", "password": "definitely-wrong"}
    )
    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["detail"] == wrong_password.json()["detail"]


def test_founder_account_is_the_single_fixed_row(client, db_session):
    # Bootstrap already ran (the `client` fixture's `with TestClient(app)` triggers FastAPI's
    # startup event) — confirms app/bootstrap.py's bootstrap_founder_user() provisioned
    # exactly the row app/deps.py's require_founder() checks against, not just *a* row with
    # role=founder.
    founder = db_session.get(User, FOUNDER_USER_ID)
    assert founder is not None
    assert founder.role == UserRole.founder
    assert founder.email_verified is True


def test_founder_can_log_in_and_use_the_whole_system(client):
    login = client.post(
        "/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"}
    )
    assert login.status_code == 200
    body = login.json()
    assert body["role"] == "founder"
    csrf = body["csrf_token"]

    for path in ["/api/conversations", "/api/documents", "/api/projects", "/api/admin/providers/status"]:
        res = client.get(path)
        assert res.status_code == 200, f"{path} should be reachable for the founder, got {res.status_code}"

    # /api/chat and /api/knowledge/search aren't exercised here — both call out to a real
    # embedding/chat provider (see app/rag/retrieve.py, app/providers/openai_provider.py),
    # which this test env has no working credentials or network access for. The
    # require_founder gate on those two routes is still exercised (the dependency runs
    # before the route body does), just via the 403 side in
    # test_non_founder_denied_every_protected_route above and via frontend/e2e/auth.spec.ts's
    # real chat round-trip (against a faked provider — see backend/scripts/run_e2e_backend.py).
    project_res = client.post(
        "/api/projects", json={"name": "founder-only smoke test", "status": "active"}, headers={"X-CSRF-Token": csrf}
    )
    assert project_res.status_code == 200

    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 200
    assert me_res.json()["email"] == "founder@lifeos.local"


def test_password_reset_works_for_founder(client, sent_emails):
    login = client.post(
        "/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"}
    )
    assert login.status_code == 200

    forgot = client.post("/api/auth/forgot-password", json={"email": "founder@lifeos.local"})
    assert forgot.status_code == 202
    reset_email = next(e for e in sent_emails if e["to"] == "founder@lifeos.local")
    token = re.search(r"token=(\S+)", reset_email["body"]).group(1)

    new_password = "BrandNewSecurePassword456!"
    reset = client.post("/api/auth/reset-password", json={"token": token, "new_password": new_password})
    assert reset.status_code == 200

    # Old session is dead (reset revokes all sessions)...
    assert client.get("/api/auth/me").status_code == 401

    # A real client would never hit this, but a test can: JWT `iat` has whole-second
    # precision (see app/deps.py's get_current_user), and reset-password just bumped
    # sessions_valid_after to "now" — logging back in within the same wall-clock second would
    # mint a token whose iat collides with that same second and gets treated as pre-revoked
    # by design (documented fail-closed behavior in app/deps.py, not a bug). Crossing the
    # second boundary here is the same thing a human re-typing their new password would do
    # for free.
    import time

    time.sleep(1)

    # ...but the founder can log back in with the new password and still reach MainAI.
    relogin = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": new_password})
    assert relogin.status_code == 200
    assert relogin.json()["role"] == "founder"
    assert client.get("/api/conversations").status_code == 200
