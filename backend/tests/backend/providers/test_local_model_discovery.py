"""Live local-model discovery contract.

No real Ollama process is required: the HTTP boundary is replaced with deterministic
payloads. The endpoint test proves founder authorization and the fail-soft runtime-down
contract; it does not claim that a model was installed or verified.
"""

import pytest

from app.providers.local_discovery import (
    LocalDiscoveryError,
    LocalModel,
    parse_local_models,
)

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_parse_local_models_is_bounded_deduplicated_and_sorted():
    models = parse_local_models(
        {
            "models": [
                {"name": "qwen2.5:0.5b", "size": 397_000_000, "digest": "abc"},
                {"model": "llama3.1:latest", "size": 4_700_000_000},
                {"name": "qwen2.5:0.5b", "size": 1},
                {"name": "  ", "size": 1},
                {"name": "bad\nname"},
                {"name": "nomic-embed-text", "size": -1, "modified_at": "2026-11-22T10:00:00Z"},
            ]
        }
    )
    assert [model.name for model in models] == [
        "llama3.1:latest",
        "nomic-embed-text",
        "qwen2.5:0.5b",
    ]
    assert models[0].size_bytes == 4_700_000_000
    assert models[1].size_bytes is None
    assert models[2].digest == "abc"


def test_parse_local_models_rejects_an_unusable_body():
    with pytest.raises(LocalDiscoveryError):
        parse_local_models({"not_models": []})


def test_local_model_endpoint_reports_installed_models_and_active_identity(
    client, monkeypatch
):
    async def _fake_discovery(base_url, *, timeout=2.0):
        return [
            LocalModel(name="llama3.1:latest", size_bytes=4_700_000_000),
            LocalModel(name="nomic-embed-text:latest", size_bytes=274_000_000),
            LocalModel(name="custom-local:latest"),
        ]

    monkeypatch.setattr("app.routers.admin.discover_local_models", _fake_discovery)
    _login(client)
    response = client.get("/api/admin/providers/local-models")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runtime"] == "ollama"
    assert body["reachable"] is True
    assert body["selection_requires_verification"] is True
    assert body["active_chat_provider"] in {"openai", "ollama"}
    assert body["active_chat_model"]
    by_name = {model["name"]: model for model in body["models"]}
    assert by_name["llama3.1:latest"]["catalog_known"] is True
    assert "chat" in by_name["llama3.1:latest"]["declared_capabilities"]
    assert by_name["nomic-embed-text:latest"]["catalog_known"] is True
    assert "embedding" in by_name["nomic-embed-text:latest"]["declared_capabilities"]
    assert by_name["custom-local:latest"]["catalog_known"] is False
    assert by_name["custom-local:latest"]["declared_capabilities"] == []


def test_local_model_endpoint_fails_soft_when_runtime_is_down(client, monkeypatch):
    async def _down(base_url, *, timeout=2.0):
        raise LocalDiscoveryError("runtime down")

    monkeypatch.setattr("app.routers.admin.discover_local_models", _down)
    _login(client)
    response = client.get("/api/admin/providers/local-models")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reachable"] is False
    assert body["models"] == []
    assert body["active_chat_model"]


def test_local_model_discovery_requires_founder(client):
    response = client.get("/api/admin/providers/local-models")
    assert response.status_code in {401, 403}
