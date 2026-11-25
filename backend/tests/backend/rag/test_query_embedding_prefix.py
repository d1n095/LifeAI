"""Query-side embedding prefix (`EMBEDDING_QUERY_PREFIX`).

Instruction-aware local embedding models (Qwen3-Embedding and similar) are trained
asymmetrically: the retrieval QUERY carries an instruction prefix, documents stay bare. Using
them without the prefix does not error — it just ranks worse, which is the worst kind of
failure for a local knowledge base because nothing looks broken.

These tests pin three things:
  - the default configuration is a byte-identical pass-through (symmetric models, and every
    already-indexed embedding space, keep working unchanged);
  - the prefix reaches the QUERY embedding call;
  - the independent text-match channel keeps the RAW query, so this is not a second retrieval
    system — it is the correct input for the existing one.
"""

from __future__ import annotations

import types
import uuid

import pytest

from app.rag import query_embedding
from app.rag.retrieve import retrieve_context

PREFIX = "Instruct: Given a question, retrieve passages that answer it.\nQuery: "


def _settings(prefix: str):
    return types.SimpleNamespace(embedding_query_prefix=prefix)


def test_default_is_a_byte_identical_passthrough(monkeypatch):
    monkeypatch.setattr(query_embedding, "get_settings", lambda: _settings(""))
    assert query_embedding.prepare_embedding_query("Vad är planen?") == "Vad är planen?"


def test_configured_prefix_is_prepended_to_the_query(monkeypatch):
    monkeypatch.setattr(query_embedding, "get_settings", lambda: _settings(PREFIX))
    assert query_embedding.prepare_embedding_query("Vad är planen?") == f"{PREFIX}Vad är planen?"


@pytest.mark.asyncio
async def test_retrieve_embeds_the_prefixed_query_and_keeps_the_active_space(monkeypatch):
    captured: dict = {}

    async def fake_embed(db, provider, texts, **kwargs):
        captured["texts"] = list(texts)
        return [[0.0] * 768]

    def fake_search(db, owner_id, vector, **kwargs):
        captured["search_kwargs"] = kwargs
        return []

    monkeypatch.setattr("app.rag.retrieve.embed_with_policy", fake_embed)
    monkeypatch.setattr("app.rag.retrieve.search", fake_search)
    monkeypatch.setattr(
        "app.rag.retrieve.resolve_active",
        lambda db, role: (types.SimpleNamespace(name="ollama"), "nomic-embed-text"),
    )
    monkeypatch.setattr(query_embedding, "get_settings", lambda: _settings(PREFIX))

    await retrieve_context(db=None, owner_id=uuid.uuid4(), query="Vad är planen?")

    assert captured["texts"] == [f"{PREFIX}Vad är planen?"]
    # The active embedding space still travels with the search, so retrieval cannot mix
    # vectors from a different model (app/rag/embedding_space.py).
    assert captured["search_kwargs"]["embedding_model"] == "nomic-embed-text"
    assert captured["search_kwargs"]["embedding_native_dim"] == 768
