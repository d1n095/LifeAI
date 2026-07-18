"""Rate limiting is backed by Redis in this test run too (see tests/conftest.py's
REDIS_URL) — the same code path production uses, not a divergent in-memory-only path."""


def test_login_is_rate_limited_per_ip(client):
    statuses = []
    for _ in range(15):
        res = client.post("/api/auth/login", json={"email": "nonexistent@example.com", "password": "wrong-password"})
        statuses.append(res.status_code)
    assert 429 in statuses


def test_repeated_failed_logins_against_one_account_are_blocked_even_before_the_ip_limit(client, make_verified_user):
    """The per-account brute-force counter (10 failed attempts / 15 min, keyed by email —
    see app/routers/auth.py) is independent of, and can trip before, the per-IP rate limit,
    which matters against a distributed attack spread across many source IPs."""
    make_verified_user(email="bruteforce@example.com")
    statuses = []
    for _ in range(12):
        res = client.post("/api/auth/login", json={"email": "bruteforce@example.com", "password": "definitely-wrong"})
        statuses.append(res.status_code)
    assert 429 in statuses


def test_register_is_rate_limited_per_ip(client):
    statuses = []
    for i in range(10):
        res = client.post(
            "/api/auth/register",
            json={"email": f"ratelimited{i}@example.com", "password": "GoodPassword1234!", "website": ""},
        )
        statuses.append(res.status_code)
    assert 429 in statuses


def test_forgot_password_is_rate_limited_per_ip(client):
    statuses = []
    for _ in range(10):
        res = client.post("/api/auth/forgot-password", json={"email": "someone@example.com"})
        statuses.append(res.status_code)
    assert 429 in statuses


def test_account_deletion_is_rate_limited(client, make_verified_user):
    user, password = make_verified_user(email="ratelimiteddelete@example.com")
    login = client.post("/api/auth/login", json={"email": "ratelimiteddelete@example.com", "password": password})
    csrf = login.json()["csrf_token"]

    statuses = []
    for _ in range(8):
        res = client.request("DELETE", "/api/account", json={"password": "wrong"}, headers={"X-CSRF-Token": csrf})
        statuses.append(res.status_code)
    assert 429 in statuses
