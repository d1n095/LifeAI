"""DB-backed integration tests for the passkey HTTP endpoints (/api/auth/webauthn/*).

Drives the REAL FastAPI routes via TestClient against the real Postgres harness (alembic
migration + RLS), with soft_webauthn as the platform authenticator. Covers the credential
lifecycle (enroll -> list -> passkey login), RLS owner isolation, the step-up on revocation,
and single-use challenges.

User verification is relaxed for these tests only (soft_webauthn cannot assert UV); the
UV-required gate itself is proven in tests/test_webauthn_service.py and stays True in prod.
"""

import base64

import pytest
from soft_webauthn import SoftWebauthnDevice

import app.routers.webauthn as war

ORIGIN = "http://localhost:3000"  # matches settings.webauthn_rp_origin default
PW = "CorrectHorseBattery9!"


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _relax_uv(monkeypatch):
    monkeypatch.setattr(war, "REQUIRE_UV", False)


def _login(client, email, password=PW):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def _enroll(client, csrf, device, label="Test Passkey"):
    r = client.post("/api/auth/webauthn/register/begin", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    data = r.json()
    o = data["options"]
    att = device.create(
        {
            "publicKey": {
                "rp": o["rp"],
                "user": {
                    "id": _b64d(o["user"]["id"]),
                    "name": o["user"]["name"],
                    "displayName": o["user"]["displayName"],
                },
                "challenge": _b64d(o["challenge"]),
                "pubKeyCredParams": o["pubKeyCredParams"],
            }
        },
        ORIGIN,
    )
    cred = {
        "id": _b64e(att["rawId"]),
        "rawId": _b64e(att["rawId"]),
        "response": {
            "clientDataJSON": _b64e(att["response"]["clientDataJSON"]),
            "attestationObject": _b64e(att["response"]["attestationObject"]),
        },
        "type": "public-key",
        "clientExtensionResults": {},
    }
    return client.post(
        "/api/auth/webauthn/register/complete",
        headers={"X-CSRF-Token": csrf},
        json={"handle": data["handle"], "credential": cred, "label": label},
    ), data["handle"], cred


def _auth_begin(client, email):
    r = client.post("/api/auth/webauthn/authenticate/begin", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()


def _assert_from_device(device, options):
    ast = device.get(
        {
            "publicKey": {
                "challenge": _b64d(options["challenge"]),
                "rpId": options["rpId"],
                "allowCredentials": [
                    {"id": _b64d(c["id"]), "type": "public-key"} for c in options.get("allowCredentials", [])
                ],
            }
        },
        ORIGIN,
    )
    cred = {
        "id": _b64e(ast["rawId"]),
        "rawId": _b64e(ast["rawId"]),
        "response": {
            "authenticatorData": _b64e(ast["response"]["authenticatorData"]),
            "clientDataJSON": _b64e(ast["response"]["clientDataJSON"]),
            "signature": _b64e(ast["response"]["signature"]),
        },
        "type": "public-key",
        "clientExtensionResults": {},
    }
    if ast["response"].get("userHandle"):
        cred["response"]["userHandle"] = _b64e(ast["response"]["userHandle"])
    return cred


def test_enroll_list_and_passkey_login(client, make_verified_user):
    user, _ = make_verified_user(email="pk-a@example.com", password=PW)
    csrf = _login(client, user.email)
    device = SoftWebauthnDevice()

    res, _, _ = _enroll(client, csrf, device, label="MacBook Touch ID")
    assert res.status_code == 200, res.text

    lst = client.get("/api/auth/webauthn/credentials")
    assert lst.status_code == 200
    creds = lst.json()
    assert len(creds) == 1 and creds[0]["label"] == "MacBook Touch ID"

    # Log out of the password session; log back in with the passkey only.
    client.cookies.clear()
    data = _auth_begin(client, user.email)
    cred = _assert_from_device(device, data["options"])
    done = client.post(
        "/api/auth/webauthn/authenticate/complete",
        json={"handle": data["handle"], "credential": cred},
    )
    assert done.status_code == 200, done.text
    assert done.json()["email"] == user.email
    # The passkey login produced a real session.
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == user.email


def test_rls_isolation_between_users(client, make_verified_user):
    a, _ = make_verified_user(email="pk-owner@example.com", password=PW)
    b, _ = make_verified_user(email="pk-other@example.com", password=PW)

    csrf_a = _login(client, a.email)
    device = SoftWebauthnDevice()
    res, _, _ = _enroll(client, csrf_a, device)
    assert res.status_code == 200
    a_cred_id = client.get("/api/auth/webauthn/credentials").json()[0]["id"]

    # User B sees none of A's credentials, and cannot delete A's credential.
    client.cookies.clear()
    csrf_b = _login(client, b.email)
    assert client.get("/api/auth/webauthn/credentials").json() == []
    delete_other = client.request(
        "DELETE",
        f"/api/auth/webauthn/credentials/{a_cred_id}",
        headers={"X-CSRF-Token": csrf_b},
        json={"password": PW},
    )
    assert delete_other.status_code == 404  # RLS + owner filter hides A's row from B


def test_delete_requires_password_stepup(client, make_verified_user):
    user, _ = make_verified_user(email="pk-del@example.com", password=PW)
    csrf = _login(client, user.email)
    device = SoftWebauthnDevice()
    _enroll(client, csrf, device)
    cred_id = client.get("/api/auth/webauthn/credentials").json()[0]["id"]

    # Wrong password -> refused.
    bad = client.request(
        "DELETE",
        f"/api/auth/webauthn/credentials/{cred_id}",
        headers={"X-CSRF-Token": csrf},
        json={"password": "not-the-password"},
    )
    assert bad.status_code == 403
    assert len(client.get("/api/auth/webauthn/credentials").json()) == 1

    # Correct password -> removed.
    ok = client.request(
        "DELETE",
        f"/api/auth/webauthn/credentials/{cred_id}",
        headers={"X-CSRF-Token": csrf},
        json={"password": PW},
    )
    assert ok.status_code == 204
    assert client.get("/api/auth/webauthn/credentials").json() == []

    # After revocation, the passkey can no longer authenticate.
    client.cookies.clear()
    data = _auth_begin(client, user.email)
    cred = _assert_from_device(device, data["options"])
    done = client.post(
        "/api/auth/webauthn/authenticate/complete",
        json={"handle": data["handle"], "credential": cred},
    )
    assert done.status_code == 401


def test_challenge_is_single_use(client, make_verified_user):
    user, _ = make_verified_user(email="pk-replay@example.com", password=PW)
    csrf = _login(client, user.email)
    device = SoftWebauthnDevice()
    res, handle, cred = _enroll(client, csrf, device)
    assert res.status_code == 200
    # Replaying the same registration handle is rejected (challenge already consumed).
    replay = client.post(
        "/api/auth/webauthn/register/complete",
        headers={"X-CSRF-Token": csrf},
        json={"handle": handle, "credential": cred, "label": "replay"},
    )
    assert replay.status_code == 400
