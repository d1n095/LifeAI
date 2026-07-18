"""Session/cookie/CSRF mechanics at the HTTP layer. Complements (doesn't replace) the
Playwright suite (frontend/e2e/) which exercises the parts that genuinely need a real
browser — JS's inability to read HttpOnly cookies, a real cross-origin CSRF attempt. These
tests cover everything that's actually about server-side logic: does refresh rotate, does
replay get detected, is CSRF enforced, does logout/logout-all revoke."""


def _raw_set_cookie_headers(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def test_login_sets_httponly_secure_samesite_none_cookies(client):
    res = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    assert res.status_code == 200
    headers = _raw_set_cookie_headers(res)
    access_header = next(h for h in headers if h.startswith("access_token="))
    refresh_header = next(h for h in headers if h.startswith("refresh_token="))
    for header in (access_header, refresh_header):
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "SameSite=none" in header.lower().replace(" ", "") or "samesite=none" in header.lower()
    assert "Path=/api/auth" in refresh_header
    assert "csrf_token" in res.json()


def test_wrong_password_rejected(client):
    res = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "wrong-password"})
    assert res.status_code == 401


def test_me_requires_session(client):
    res = client.get("/api/auth/me")
    assert res.status_code == 401


def test_mutating_request_without_csrf_header_rejected(client):
    login = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    assert login.status_code == 200
    res = client.post("/api/projects", json={"name": "no csrf header", "status": "active"})
    assert res.status_code == 403


def test_mutating_request_with_correct_csrf_header_succeeds(client):
    login = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    csrf = login.json()["csrf_token"]
    res = client.post(
        "/api/projects", json={"name": "with csrf header", "status": "active"}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200


def test_mutating_request_with_wrong_csrf_header_rejected(client):
    client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    res = client.post(
        "/api/projects", json={"name": "wrong csrf", "status": "active"}, headers={"X-CSRF-Token": "not-the-real-value"}
    )
    assert res.status_code == 403


def test_refresh_rotates_token_and_csrf(client):
    login = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    csrf1 = login.json()["csrf_token"]
    refresh_cookie_1 = client.cookies.get("refresh_token")

    res = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf1})
    assert res.status_code == 200
    csrf2 = res.json()["csrf_token"]
    refresh_cookie_2 = client.cookies.get("refresh_token")

    assert csrf2 != csrf1
    assert refresh_cookie_2 != refresh_cookie_1


def test_replaying_a_rotated_refresh_token_is_rejected_and_revokes_the_family(client):
    login = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    csrf1 = login.json()["csrf_token"]
    old_refresh_value = client.cookies.get("refresh_token")

    refresh1 = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf1})
    csrf2 = refresh1.json()["csrf_token"]
    new_refresh_value = client.cookies.get("refresh_token")
    assert new_refresh_value != old_refresh_value

    # Put the OLD (already-rotated-away) refresh cookie back and replay it.
    client.cookies.set("refresh_token", old_refresh_value)
    replay = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf1})
    assert replay.status_code == 401

    # The token that replaced it (otherwise still valid in isolation, and not the one that
    # was replayed) must ALSO now be dead — full-family revocation, not just rejection of
    # the specific replayed token. Restore it explicitly since the 401 above cleared cookies.
    client.cookies.set("refresh_token", new_refresh_value)
    still_valid_looking = client.post("/api/auth/refresh", headers={"X-CSRF-Token": csrf2})
    assert still_valid_looking.status_code == 401


def test_logout_revokes_access_token_immediately(client):
    login = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    csrf = login.json()["csrf_token"]

    before = client.get("/api/auth/me")
    assert before.status_code == 200

    logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert logout.status_code == 200

    after = client.get("/api/auth/me")
    assert after.status_code == 401


def test_logout_all_revokes_every_session_not_just_the_caller(client, make_verified_user):
    from fastapi.testclient import TestClient

    from app.main import app

    user, password = make_verified_user()

    with TestClient(app, base_url="https://testserver") as device_a, TestClient(
        app, base_url="https://testserver"
    ) as device_b:
        login_a = device_a.post("/api/auth/login", json={"email": user.email, "password": password})
        login_b = device_b.post("/api/auth/login", json={"email": user.email, "password": password})
        assert login_a.status_code == 200
        assert login_b.status_code == 200

        csrf_a = login_a.json()["csrf_token"]
        logout_all = device_a.post("/api/auth/logout-all", headers={"X-CSRF-Token": csrf_a})
        assert logout_all.status_code == 200

        assert device_a.get("/api/auth/me").status_code == 401
        assert device_b.get("/api/auth/me").status_code == 401


def test_password_reset_revokes_all_sessions(client, db_session, make_verified_user, sent_emails):
    import re

    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200

    forgot = client.post("/api/auth/forgot-password", json={"email": user.email})
    assert forgot.status_code == 202
    reset_email = next(e for e in sent_emails if e["to"] == user.email)
    token = re.search(r"token=(\S+)", reset_email["body"]).group(1)

    reset = client.post("/api/auth/reset-password", json={"token": token, "new_password": "BrandNewPassword456!"})
    assert reset.status_code == 200

    # The session established BEFORE the reset must now be dead.
    assert client.get("/api/auth/me").status_code == 401
