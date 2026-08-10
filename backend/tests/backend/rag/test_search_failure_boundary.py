"""PR B — search failure boundary: an embedding-provider failure during GET
/api/library/search/hybrid must degrade to the existing local text-match (ILIKE) channel and
say so explicitly, never a generic 500 and never a silent claim that semantic search ran.
See app/rag/vector_store.py's hybrid_search(vector=None) and app/routers/library.py's
search_library()."""

import httpx
import pytest

from app.config import get_settings
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _import_via_worker(client, csrf: str, filename: str, content: bytes) -> dict:
    import asyncio
    import time

    from app.worker import Worker

    res = client.post("/api/library/import", files={"file": (filename, content, "text/plain")}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        asyncio.run(Worker().run_once())
        job = client.get(f"/api/library/jobs/{job_id}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"Import job {job_id} did not reach a terminal status")


@pytest.fixture(autouse=True)
def _fake_embed_ok(monkeypatch):
    """Baseline: embedding + claim-extraction chat both succeed, so import itself always
    works regardless of what an individual test does to the embed call afterwards during
    search."""
    from app.providers.base import ChatResult

    dim = get_settings().embedding_dim

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.02] * dim for _ in texts]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def test_normal_hybrid_search_when_provider_is_healthy(client):
    """Baseline: nothing changed about the happy path — semantic_search_available is true
    and degraded_reason is absent."""
    csrf = _login(client)
    _import_via_worker(client, csrf, "healthy.txt", b"Det ovanliga ordet kringelbjorn finns bara har.")

    res = client.get("/api/library/search/hybrid", params={"q": "kringelbjorn"})
    assert res.status_code == 200
    body = res.json()
    assert body["semantic_search_available"] is True
    assert body["degraded_reason"] is None
    assert any(h["text_match"] for h in body["results"])


def test_provider_unavailable_falls_back_to_text_search(client, monkeypatch):
    """The embedding provider raising ProviderError (e.g. missing key) must not 500 — the
    text-match channel still finds the exact term, and the response says semantic search did
    not run."""
    from app.providers.base import ProviderError

    csrf = _login(client)
    _import_via_worker(client, csrf, "unavailable.txt", b"Det ovanliga ordet plimsollvind finns bara har.")

    async def _broken_embed(self, texts, model, **kwargs):
        raise ProviderError("OpenAI API-nyckel saknas.")

    monkeypatch.setattr(OpenAIProvider, "embed", _broken_embed)

    res = client.get("/api/library/search/hybrid", params={"q": "plimsollvind"})
    assert res.status_code == 200
    body = res.json()
    assert body["semantic_search_available"] is False
    assert body["degraded_reason"]
    # Never leak the raw provider exception text into the response.
    assert "nyckel" not in body["degraded_reason"].lower()
    assert any(h["document_id"] for h in body["results"] if h["text_match"])


def test_provider_timeout_falls_back_to_text_search(client, monkeypatch):
    """A provider timeout is a different failure mode than a hard error, but must degrade
    exactly the same way — real text results, explicit degraded metadata, no 500."""
    csrf = _login(client)
    _import_via_worker(client, csrf, "timeout.txt", b"Det ovanliga ordet fnurrelampa finns bara har.")

    async def _timeout_embed(self, texts, model, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(OpenAIProvider, "embed", _timeout_embed)

    res = client.get("/api/library/search/hybrid", params={"q": "fnurrelampa"})
    assert res.status_code == 200
    body = res.json()
    assert body["semantic_search_available"] is False
    assert body["degraded_reason"]
    assert any(h["document_id"] for h in body["results"] if h["text_match"])


def test_empty_semantic_results_is_not_reported_as_degraded(client):
    """Zero semantic hits is a normal outcome (nothing in the library is similar enough),
    not a provider failure — it must never be confused with the degraded path."""
    csrf = _login(client)
    _import_via_worker(client, csrf, "unrelated.txt", b"Helt ovidkommande innehall om tradgardsskotsel.")

    res = client.get("/api/library/search/hybrid", params={"q": "kvantdatorarkitektur-som-inte-finns-nagonstans"})
    assert res.status_code == 200
    body = res.json()
    assert body["semantic_search_available"] is True
    assert body["degraded_reason"] is None
    assert body["results"] == [] or all(not h["text_match"] for h in body["results"])
