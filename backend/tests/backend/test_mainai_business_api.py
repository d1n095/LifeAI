"""Founder-facing business-workflow API (app/routers/mainai_business.py).

Through the real FastAPI TestClient against real Postgres (RLS included) — proves the
executive workflow is wired into the running Life application, founder-gated, and that the
consequential publish step is fail-closed at the HTTP boundary. No provider is invoked.
"""

from __future__ import annotations

import uuid

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def test_every_endpoint_requires_founder(client):
    assert client.post("/api/mainai/business/ecommerce-assessment", json={}).status_code in (401, 403)
    assert client.get(f"/api/mainai/business/{uuid.uuid4()}").status_code in (401, 403)


def test_run_without_approval_blocks_publish_then_view_reassembles(client):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    res = client.post(
        "/api/mainai/business/ecommerce-assessment",
        json={"approve_publish": False},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    wf = body["workflow_id"]
    assert "publish_assessment" in body["blocked_tasks"]
    assert body["assessment"]["published"] is False
    assert body["assessment"]["recommendation"] == "conditional-go"
    assert body["provider_invoked"] is False

    # The Founder reads the whole project back through the one interface.
    got = client.get(f"/api/mainai/business/{wf}")
    assert got.status_code == 200, got.text
    view = got.json()
    assert view["exists"] is True
    assert view["assessment"]["published"] is False
    assert len(view["tasks"]) == 5


def test_run_with_approval_publishes(client):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    res = client.post(
        "/api/mainai/business/ecommerce-assessment",
        json={"approve_publish": True, "founder_approval_ref": "founder-approval:api-test"},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["assessment"]["published"] is True
    assert "publish_assessment" in body["approvals_satisfied"]


def test_unknown_workflow_is_404(client):
    _login(client)
    res = client.get(f"/api/mainai/business/{uuid.uuid4()}")
    assert res.status_code == 404, res.text
