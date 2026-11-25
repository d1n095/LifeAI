"""Real HTTP/worker coverage for explicit Library source replacement (F11/F22).

The replacement flow is deliberately not a unit test of an ORM helper: it goes through the
real founder-authenticated endpoint, the real durable ImportJob, the real worker claim loop,
the real pgvector/text retrieval path and the real RLS-scoped database. Only the external
embedding/chat provider is faked, exactly like the rest of the Library integration suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
import uuid
from pathlib import Path

import pytest

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"

_APPLY_RUNTIME_PRIVILEGES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts"
    / "security"
    / "apply_runtime_privileges.py"
)


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """The replacement endpoint uses the canonical delete_if_unreferenced() path, which calls
    storage_key_still_referenced_global(). mainai_app receives EXECUTE on that function via
    the same runtime privilege policy production applies; tests/conftest.py's blanket table
    grants do not cover function EXECUTE, so apply the real policy first (same fixture as
    tests/backend/rag/test_library_routes.py)."""
    from app.config import get_settings

    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_and_verify(get_settings().database_url)


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    from app.config import get_settings
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    dim = get_settings().embedding_dim

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.03] * dim for _ in texts]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(
            content="[]",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _run_worker_once() -> bool:
    from app.worker import Worker

    return asyncio.run(Worker().run_once())


def _wait_for_job(client, job_id: str, *, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _run_worker_once()
        res = client.get(f"/api/library/jobs/{job_id}")
        assert res.status_code == 200, res.text
        job = res.json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"Import job {job_id} did not reach a terminal status within {timeout}s")


def _import_and_wait(client, csrf: str, filename: str, content: bytes) -> dict:
    res = client.post(
        "/api/library/import",
        files={"file": (filename, content, "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    return _wait_for_job(client, res.json()["id"])


def test_replace_source_creates_next_version_and_invalidates_old_retrieval(client):
    csrf = _login(client)
    old_job = _import_and_wait(
        client,
        csrf,
        "policy.txt",
        b"GAMMALTERM77 beskriver den gamla policyn. Detta ar version ett.",
    )
    old_id = old_job["file_results"][0]["source_id"]
    old_before = client.get(f"/api/library/{old_id}")
    assert old_before.status_code == 200
    assert old_before.json()["version_number"] == 1
    assert old_before.json()["chunk_preview"]

    replaced = client.post(
        f"/api/library/{old_id}/replace?confirm=true",
        files={
            "file": (
                "policy-v2.txt",
                b"NYTERM88 beskriver den nya policyn. Detta ar version tva.",
                "text/plain",
            )
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    new_id = body["source"]["id"]
    assert new_id != old_id
    assert body["superseded_source_id"] == old_id
    assert body["version_number"] == 2
    assert body["source"]["version_number"] == 2
    assert body["job"]["status"] == "pending"

    job = _wait_for_job(client, body["job"]["id"])
    assert job["status"] == "completed", job
    assert job["succeeded_count"] == 1

    new_detail = client.get(f"/api/library/{new_id}").json()
    assert new_detail["version_number"] == 2
    assert new_detail["active_truth_status"] == "active"
    assert new_detail["chunk_preview"]
    assert any(
        r["relationship_type"] == "supersedes" and r["to_source_id"] == old_id
        for r in new_detail["relationships"]
    )

    old_after = client.get(f"/api/library/{old_id}").json()
    assert old_after["active_truth_status"] == "superseded"
    # Provenance survives: the old chunks and its version history are still readable in the
    # detail view. It is retrieval, not history, that must be invalidated.
    assert old_after["chunk_preview"]
    assert old_after["version_number"] == 1
    assert any(
        r["relationship_type"] == "supersedes" and r["from_source_id"] == new_id
        for r in old_after["relationships"]
    )

    old_hits = client.get("/api/library/search/hybrid", params={"q": "GAMMALTERM77"}).json()["results"]
    assert all(hit["document_id"] != old_id for hit in old_hits)
    new_hits = client.get("/api/library/search/hybrid", params={"q": "NYTERM88"}).json()["results"]
    assert any(hit["document_id"] == new_id for hit in new_hits)

    # A second replacement of the same old source is refused, not silently forked.
    again = client.post(
        f"/api/library/{old_id}/replace?confirm=true",
        files={"file": ("policy-v3.txt", b"TREDJETERM99 annu en version", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert again.status_code == 409


def test_replace_requires_explicit_confirmation(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "confirm.txt", b"innehall for bekraftelse")
    source_id = job["file_results"][0]["source_id"]

    res = client.post(
        f"/api/library/{source_id}/replace",
        files={"file": ("confirm-v2.txt", b"nytt innehall", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 400
    assert "bekräftelse" in res.json()["detail"].lower()


def test_replace_identical_bytes_is_refused_without_creating_a_version(client):
    csrf = _login(client)
    content = b"IDENTISKTERM42 oforandrat innehall"
    job = _import_and_wait(client, csrf, "identical.txt", content)
    source_id = job["file_results"][0]["source_id"]

    res = client.post(
        f"/api/library/{source_id}/replace?confirm=true",
        files={"file": ("identical-again.txt", content, "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 409
    detail = client.get(f"/api/library/{source_id}").json()
    assert detail["active_truth_status"] == "active"
    assert detail["version_number"] == 1
    assert not any(r["relationship_type"] == "supersedes" for r in detail["relationships"])


def test_replace_with_content_already_owned_by_another_source_is_refused(client):
    csrf = _login(client)
    first = _import_and_wait(client, csrf, "first.txt", b"FORSTATERM11 unikt innehall")
    second = _import_and_wait(client, csrf, "second.txt", b"ANDRA TERM22 annat innehall")
    first_id = first["file_results"][0]["source_id"]

    res = client.post(
        f"/api/library/{first_id}/replace?confirm=true",
        files={"file": ("first-v2.txt", b"ANDRA TERM22 annat innehall", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 409
    detail = client.get(f"/api/library/{first_id}").json()
    assert detail["active_truth_status"] == "active"
    assert detail["version_number"] == 1
    assert second["file_results"][0]["source_id"] != first_id


def test_replace_route_requires_founder(client, make_verified_user):
    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]

    res = client.post(
        f"/api/library/{uuid.uuid4()}/replace?confirm=true",
        files={"file": ("denied.txt", b"not allowed", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 403
