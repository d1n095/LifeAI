"""Public /api/health: worker liveness and local-model readiness, without 503'ing a live API.

The `local_model` block exists because a local-first deployment has failure modes a database
ping cannot see: the runtime was restarted, or a model was never pulled. Before this, the only
way to find out was to open a chat and read a provider error. The contract these tests pin:

  - a cloud-only deployment's payload is byte-identical to before (no new field);
  - `reachable` is TRI-STATE — True / False / None ("no model configured, so nothing was
    probed"), never a guess;
  - `missing_models` is present only when the runtime actually answered: on an unreachable
    runtime "these models are missing" is unknowable, so the key is omitted instead of filled
    with the configured list;
  - a down local runtime never turns a live API unhealthy.
"""

from __future__ import annotations

import types

import httpx
import pytest

from app.routers import health as health_router


# ------------------------------------------------------------------------------ worker field

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


# ------------------------------------------------------------------------- local model field

def _active(chat=("openai", "gpt-4o-mini"), embedding=("openai", "text-embedding-3-small")):
    """Stub app.providers.registry.resolve_active for both roles."""

    def resolve_active(db, role=None):
        provider_name, model = chat if role == "chat" else embedding
        return types.SimpleNamespace(name=provider_name), model

    return resolve_active


def test_health_omits_local_model_for_a_cloud_only_deployment(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: True)
    monkeypatch.setattr("app.providers.registry.resolve_active", _active())
    res = client.get("/api/health")
    assert res.status_code == 200
    assert "local_model" not in res.json()


def test_health_reports_local_models_as_ready_when_installed(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: True)
    monkeypatch.setattr(
        "app.providers.registry.resolve_active",
        _active(chat=("ollama", "llama3.1"), embedding=("ollama", "nomic-embed-text")),
    )
    monkeypatch.setattr(
        health_router,
        "_fetch_local_model_names",
        lambda _base_url: {"llama3.1:latest", "nomic-embed-text:latest"},
    )
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["local_model"] == {
        "runtime": "ollama",
        "reachable": True,
        "configured_models": ["llama3.1", "nomic-embed-text"],
        "missing_models": [],
    }


def test_health_reports_a_model_that_was_never_pulled(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: True)
    monkeypatch.setattr(
        "app.providers.registry.resolve_active",
        _active(chat=("ollama", "llama3.1"), embedding=("ollama", "nomic-embed-text")),
    )
    monkeypatch.setattr(health_router, "_fetch_local_model_names", lambda _base_url: {"qwen2.5:0.5b"})
    res = client.get("/api/health")
    # A missing model is a provisioning gap, not an unhealthy API.
    assert res.status_code == 200
    assert res.json()["local_model"]["reachable"] is True
    assert res.json()["local_model"]["missing_models"] == ["llama3.1", "nomic-embed-text"]


def test_health_reports_an_unreachable_runtime_without_guessing_missing_models(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: True)
    monkeypatch.setattr(
        "app.providers.registry.resolve_active",
        _active(chat=("ollama", "llama3.1"), embedding=("ollama", "nomic-embed-text")),
    )

    def _boom(_base_url):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(health_router, "_fetch_local_model_names", _boom)
    res = client.get("/api/health")
    assert res.status_code == 200
    local_model = res.json()["local_model"]
    assert local_model["reachable"] is False
    assert local_model["configured_models"] == ["llama3.1", "nomic-embed-text"]
    # Nothing was checked, so nothing is claimed to be missing.
    assert "missing_models" not in local_model


def test_health_reports_unknown_reachability_when_no_model_is_configured(client, monkeypatch):
    monkeypatch.setattr(health_router, "worker_process_alive", lambda: True)
    monkeypatch.setattr(
        "app.providers.registry.resolve_active", _active(chat=("ollama", ""), embedding=("openai", "x"))
    )
    res = client.get("/api/health")
    assert res.json()["local_model"] == {"runtime": "ollama", "reachable": None, "configured_models": []}


@pytest.mark.parametrize(
    ("configured", "installed", "expected"),
    [
        ("llama3.1", {"llama3.1:latest"}, True),
        ("nomic-embed-text:latest", {"nomic-embed-text"}, True),
        ("llama3.1", {"llama3.1"}, True),
        ("ghost", {"llama3.1:latest"}, False),
    ],
)
def test_installed_model_matching_tolerates_ollama_tag_shorthand(configured, installed, expected):
    assert health_router._is_installed(installed, configured) is expected
