"""R15 RAG failure boundaries: a broken embedding path is never an unhandled 500.

These are DB-free and exercise the real ``retrieve_context()`` policy. They cover the two
failure shapes that previously escaped its ``ProviderError``-only catch:

* an unwrapped ``httpx`` transport error from a configured-but-failing embedding provider;
* a provider that returns an empty batch or an empty vector (``vectors[0]`` used to raise
  ``IndexError`` in every caller).

Both must surface as an explicit ``RetrievalUnavailableError`` (a
``GroundingUnavailableError`` subclass) so chat marks the turn ``degraded`` and knowledge /
workbench / blueprint return 503 instead of crashing or pretending retrieval ran.

    cd backend
    PYTHONPATH=. /tmp/r15-venv/bin/python -m pytest \
      tests/security_r15_successor -q \
      --confcutdir=tests/security_r15_successor
"""

import asyncio
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.providers.base import ProviderError
from app.rag import retrieve
from app.routers import knowledge


def _local_provider(monkeypatch):
    monkeypatch.setattr(
        retrieve,
        "resolve_active",
        lambda *_a, **_k: (
            SimpleNamespace(name="ollama", is_local=True),
            "nomic-embed-text",
        ),
    )


def _retrieve(monkeypatch, embed):
    _local_provider(monkeypatch)
    monkeypatch.setattr(retrieve, "embed_with_policy", embed)
    return asyncio.run(
        retrieve.retrieve_context(None, uuid.uuid4(), "query", effect_guard=True)
    )


def test_transport_failure_is_explicit_and_sanitized(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise httpx.ConnectError(
            "connection refused at https://secret.example/?key=sk-live-must-not-leak"
        )

    with pytest.raises(retrieve.RetrievalUnavailableError) as error:
        _retrieve(monkeypatch, fail)

    assert error.value.category == "unreachable"
    assert error.value.provider_request_may_have_left is True
    assert "Kunskapssökning" in str(error.value)
    assert "sk-live-must-not-leak" not in str(error.value)


def test_provider_failure_preserves_effect_boundary(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise ProviderError(
            "embedding model unavailable",
            category="unreachable",
            provider_request_may_have_left=True,
        )

    with pytest.raises(retrieve.RetrievalUnavailableError) as error:
        _retrieve(monkeypatch, fail)

    assert error.value.category == "unreachable"
    assert error.value.provider_request_may_have_left is True
    assert "embedding model unavailable" in str(error.value)


def test_empty_embedding_batch_is_explicit_not_indexerror(monkeypatch):
    async def empty(*_args, **_kwargs):
        return []

    with pytest.raises(retrieve.RetrievalUnavailableError) as error:
        _retrieve(monkeypatch, empty)

    assert "ingen vektor" in str(error.value)
    assert error.value.provider_request_may_have_left is True


def test_empty_embedding_vector_is_explicit_not_indexerror(monkeypatch):
    async def empty(*_args, **_kwargs):
        return [[]]

    with pytest.raises(retrieve.RetrievalUnavailableError) as error:
        _retrieve(monkeypatch, empty)

    assert "ingen vektor" in str(error.value)


def test_missing_embedding_provider_is_pre_egress(monkeypatch):
    def no_provider(*_args, **_kwargs):
        raise ProviderError("Okänd leverantör: missing")

    monkeypatch.setattr(retrieve, "resolve_active", no_provider)

    with pytest.raises(retrieve.RetrievalUnavailableError) as error:
        asyncio.run(
            retrieve.retrieve_context(None, uuid.uuid4(), "query", effect_guard=True)
        )

    assert error.value.provider_request_may_have_left is False
    assert "ingen embedding-leverantör" in str(error.value)


def test_guard_refusal_remains_pre_egress_grounding_unavailable(monkeypatch):
    async def deny(*_args, **_kwargs):
        raise ProviderError(
            "External query embedding requires a metered effect contract.",
            category="unsupported",
            provider_request_may_have_left=False,
        )

    monkeypatch.setattr(
        retrieve,
        "resolve_active",
        lambda *_a, **_k: (SimpleNamespace(name="openai"), "text-embedding-3-small"),
    )
    monkeypatch.setattr(retrieve, "embed_with_policy", deny)

    with pytest.raises(retrieve.GroundingUnavailableError) as error:
        asyncio.run(
            retrieve.retrieve_context(None, uuid.uuid4(), "query", effect_guard=True)
        )

    assert error.value.category == "unsupported"
    assert error.value.provider_request_may_have_left is False


def test_knowledge_search_maps_retrieval_outage_to_503(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise retrieve.RetrievalUnavailableError(
            "Kunskapssökning är inte tillgänglig: nätverksfel.",
            category="unreachable",
            provider_request_may_have_left=True,
        )

    monkeypatch.setattr(knowledge, "retrieve_context", fail)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            knowledge.search_knowledge(
                knowledge.SearchQuery(query="q"),
                db=None,
                user=SimpleNamespace(id=uuid.uuid4()),
            )
        )

    assert error.value.status_code == 503
