"""The configured model must be reachable for ordinary Supervisor work.

Regression for a real blocker found 2026-11-27: `WorkBinding.provider_adapter` is None on the
production path, so `RegistryPlanningAdapter` was built with no provider and no model.
`plan_with_provider()` refuses to reserve founder spend without an explicit pair and therefore
parked EVERY task that had no deterministic recipe as PROVIDER_SPEND_NOT_AUTHORIZED -- even with
a live grant. The configured model was structurally unreachable, so only the fixed calculator
recipes could ever execute.

These tests pin the three properties that make the fix safe:
  * the configured default is used when the caller supplies neither value;
  * an explicit provider/model is never replaced;
  * LOCAL_ONLY_MODE never resolves a non-local default, because the explicit-provider branch
    bypasses `chat_with_fallback()`'s local-only filtering.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.provider_planning import service as provider_planning_service
from app.provider_planning.service import RegistryPlanningAdapter


def _settings(monkeypatch, **values):
    stub = SimpleNamespace(
        default_llm_provider=values.get("provider", "ollama"),
        default_llm_model=values.get("model", "llama3.1"),
        local_only_mode=values.get("local_only", True),
    )
    monkeypatch.setattr("app.config.get_settings", lambda: stub)
    return stub


def test_configured_default_is_resolved_when_caller_supplies_nothing(monkeypatch):
    _settings(monkeypatch, provider="ollama", model="llama3.1", local_only=True)

    adapter = RegistryPlanningAdapter(object())

    assert adapter.provider_name == "ollama"
    assert adapter.model == "llama3.1"


def test_local_only_mode_refuses_to_resolve_an_external_default(monkeypatch):
    _settings(monkeypatch, provider="openai", model="gpt-4o-mini", local_only=True)

    adapter = RegistryPlanningAdapter(object())

    assert adapter.provider_name is None
    assert adapter.model is None


def test_external_default_is_resolved_when_local_only_mode_is_off(monkeypatch):
    _settings(monkeypatch, provider="openai", model="gpt-4o-mini", local_only=False)

    adapter = RegistryPlanningAdapter(object())

    assert adapter.provider_name == "openai"
    assert adapter.model == "gpt-4o-mini"


def test_explicit_provider_and_model_are_never_replaced(monkeypatch):
    _settings(monkeypatch, provider="ollama", model="llama3.1", local_only=True)

    adapter = RegistryPlanningAdapter(object(), provider_name="zai", model="glm-5.3")

    assert adapter.provider_name == "zai"
    assert adapter.model == "glm-5.3"


@pytest.mark.parametrize("provider,model", [(None, "llama3.1"), ("ollama", None), ("", "  ")])
def test_incomplete_configuration_resolves_to_nothing(monkeypatch, provider, model):
    _settings(monkeypatch, provider=provider, model=model, local_only=True)

    adapter = RegistryPlanningAdapter(object())

    assert adapter.provider_name is None
    assert adapter.model is None


def test_local_only_helper_exposes_the_enforced_set():
    from app.providers.registry import local_provider_names

    assert "ollama" in local_provider_names()
    assert "openai" not in local_provider_names()


def test_module_helper_is_the_single_resolution_point(monkeypatch):
    """The adapter must use the helper (not a second, drifting copy of the same rules)."""
    _settings(monkeypatch, provider="ollama", model="llama3.1", local_only=True)

    assert provider_planning_service._configured_default_provider() == ("ollama", "llama3.1")
