def test_health_check(client):
    res = client.get("/api/health")
    assert res.status_code == 200


def test_bootstrap_admin_can_log_in(client):
    res = client.post("/api/auth/login", json={"email": "admin@lifeos.local", "password": "TestAdminPassword123!"})
    assert res.status_code == 200
    body = res.json()
    assert body["email"] == "admin@lifeos.local"
    assert body["role"] == "admin"
    assert "csrf_token" in body
