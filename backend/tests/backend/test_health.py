"""Public /api/health worker field: observe worker liveness without 503'ing a live API."""

from app.routers import health as health_router


def test_health_reports_alive_when_heartbeat_is_present(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: True)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "worker": "alive"}


def test_health_reports_unknown_when_heartbeat_is_absent_without_503(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: None)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "worker": "unknown"}


def test_health_unavailable_body_has_no_worker_field(client, monkeypatch):
    monkeypatch.setattr(health_router, "_check_database", lambda _db: False)
    res = client.get("/api/health")
    assert res.status_code == 503
    assert res.json() == {"status": "unavailable"}
