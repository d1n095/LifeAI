def test_health_check(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["worker"] in ("alive", "unknown")


def test_bootstrap_founder_can_log_in(client):
    res = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"})
    assert res.status_code == 200
    body = res.json()
    assert body["email"] == "founder@lifeos.local"
    assert body["role"] == "founder"
    assert "csrf_token" in body
