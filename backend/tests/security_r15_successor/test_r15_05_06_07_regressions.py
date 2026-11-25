"""Negative regression tests for R15-05, R15-06 and R15-07.

These tests are DB-free and run in the same isolated environment as the rest of
``tests/security_r15_successor``:

* R15-05: every remaining retrieval caller must request the effect guard, and a refused
  external embedding must surface as an explicit error instead of silent lost grounding.
* R15-06: live chat must route through an exact-model policy that requires fresh
  verification for external models.
* R15-07: legitimate local custom tags remain usable, while unregistered external models
  stay fail-closed.

    cd backend
    PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
      tests/security_r15_successor -q \
      --confcutdir=tests/security_r15_successor
"""

import asyncio
import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.providers.catalog import (
    ModelSelectionError,
    ModelSelectionPolicy,
    custom_local_model_spec,
    evaluate_model,
    resolve_model_spec,
)
from app.routers import admin, blueprint, chat, knowledge, workbench
from app.rag.retrieve import GroundingUnavailableError


def test_knowledge_search_passes_effect_guard_and_owner(monkeypatch):
    captured = {}

    async def fake_retrieve(db, owner_id, query, **kwargs):
        captured["owner_id"] = owner_id
        captured["query"] = query
        captured.update(kwargs)
        return [{"text": "hit"}]

    monkeypatch.setattr(knowledge, "retrieve_context", fake_retrieve)
    owner = uuid.uuid4()

    result = asyncio.run(
        knowledge.search_knowledge(
            knowledge.SearchQuery(query="  q  "),
            db=None,
            user=SimpleNamespace(id=owner),
        )
    )

    assert result == [{"text": "hit"}]
    assert captured["owner_id"] == owner
    assert captured["query"] == "q"
    assert captured["effect_guard"] is True


def test_knowledge_search_surfaces_grounding_unavailable(monkeypatch):
    async def deny(*_args, **_kwargs):
        raise GroundingUnavailableError(
            "extern embedding saknar mätbart effektavtal",
            category="unsupported",
            provider_request_may_have_left=False,
        )

    monkeypatch.setattr(knowledge, "retrieve_context", deny)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            knowledge.search_knowledge(
                knowledge.SearchQuery(query="q"),
                db=None,
                user=SimpleNamespace(id=uuid.uuid4()),
            )
        )

    assert error.value.status_code == 503
    assert "extern embedding" in error.value.detail


def test_workbench_and_blueprint_retrieval_request_effect_guard():
    workbench_analyze = getattr(workbench.analyze, "__wrapped__", workbench.analyze)
    assert "effect_guard=True" in inspect.getsource(workbench_analyze)
    assert "effect_guard=True" in inspect.getsource(blueprint._generate)


def test_live_chat_wires_the_exact_model_policy():
    source = inspect.getsource(chat._attempt_assistant_reply)
    assert "selection_policy=_chat_selection_policy(db)" in source

    policy = chat._verified_chat_policy()
    assert policy.role == "chat"
    assert policy.allow_external is True
    assert policy.require_verified is True
    assert policy.require_verified_local is False
    assert policy.allow_custom_local is True


def test_live_chat_policy_only_applies_to_an_admin_selected_model():
    class _NoConfig:
        def query(self, *_args, **_kwargs):
            return self

        def filter_by(self, **_kwargs):
            return self

        def first(self):
            return None

    class _Configured(_NoConfig):
        def first(self):
            return object()

    assert chat._chat_selection_policy(_NoConfig()) is None
    policy = chat._chat_selection_policy(_Configured())
    assert policy is not None and policy.require_verified is True


def test_admin_verification_and_configuration_accept_custom_local_tags():
    assert "allow_custom_local=True" in inspect.getsource(admin.verify_provider_now)
    assert "allow_custom_local=True" in inspect.getsource(admin.set_provider_config)


def test_custom_local_model_spec_is_exact_local_and_free():
    spec = resolve_model_spec(
        "ollama", "founder-model:latest", role="chat", allow_custom_local=True
    )

    assert spec.provider == "ollama"
    assert spec.model_id == "founder-model:latest"
    assert spec.external is False
    assert spec.supported_capabilities == frozenset({"chat"})
    assert spec.pricing.input_per_million_usd == 0
    assert spec.pricing.output_per_million_usd == 0
    assert spec.context_window_tokens is None


def test_custom_local_embedding_spec_is_role_scoped():
    spec = resolve_model_spec(
        "ollama", "founder-embed:latest", role="embedding", allow_custom_local=True
    )
    assert spec.supported_capabilities == frozenset({"embedding"})


def test_custom_local_is_opt_in_and_external_custom_stays_closed():
    with pytest.raises(ModelSelectionError, match="unregistered"):
        resolve_model_spec("ollama", "founder-model", role="chat")

    with pytest.raises(ModelSelectionError, match="unregistered"):
        resolve_model_spec(
            "openai", "founder-model", role="chat", allow_custom_local=True
        )


def test_custom_local_spec_satisfies_a_local_only_policy():
    spec = custom_local_model_spec("ollama", "founder-model", "chat")
    allowed, reason = evaluate_model(
        spec,
        ModelSelectionPolicy(
            role="chat", allow_external=False, require_verified=False
        ),
    )
    assert allowed is True, reason
