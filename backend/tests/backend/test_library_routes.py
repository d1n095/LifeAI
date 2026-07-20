"""API-level tests for app/routers/library.py — Founder Knowledge Studio v1. Drives the
real HTTP surface (TestClient), not the orchestrator directly (see test_library_import.py
for that), so route wiring, auth/CSRF, request validation and response shaping are all
exercised together, the same way a real client would hit them."""

import io
import time
import zipfile

import pytest

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _wait_for_job(client, job_id: str, *, timeout: float = 5.0) -> dict:
    """POST /api/library/import schedules the actual work as a FastAPI BackgroundTask,
    which — unlike a plain function call — is NOT guaranteed to have finished by the time
    TestClient's .post() returns (it runs in Starlette's background-task threadpool, whose
    completion isn't awaited by the test client the way a real browser watching a job's
    status endpoint effectively is). Polling GET /jobs/{id} here is both the correct fix and
    a more realistic exercise of the same status endpoint the future Library UI polls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = client.get(f"/api/library/jobs/{job_id}")
        assert res.status_code == 200
        job = res.json()
        if job["status"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"Import job {job_id} did not reach a terminal status within {timeout}s")


def _import_and_wait(client, csrf: str, filename: str, content: bytes, content_type: str = "text/plain") -> dict:
    res = client.post("/api/library/import", files={"file": (filename, content, content_type)}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    return _wait_for_job(client, res.json()["id"])


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    from app.config import get_settings
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    dim = get_settings().embedding_dim

    async def _fake_embed(self, texts, model):
        return [[0.02] * dim for _ in texts]

    # A real import now also runs claim extraction (app/rag/claims.py, STEG 10) after
    # indexing succeeds, which calls the chat provider too — without this, OpenAIProvider's
    # is_configured() sees the truthy "fake-key-for-tests" value and would attempt a REAL
    # outbound HTTPS call to OpenAI on every import test in this file. Empty-list response
    # is deliberate: these tests don't care about claim content, just that import itself
    # still behaves correctly with claim extraction wired in.
    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_all_library_routes_require_founder(client, make_verified_user):
    """Same guarantee as test_founder_only.py's test_non_founder_denied_every_protected_route
    — a logged-in non-founder must be denied every /api/library route regardless of method."""
    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200

    assert client.get("/api/library").status_code == 403
    assert client.get("/api/library/search/hybrid?q=test").status_code == 403
    import uuid

    fake_id = uuid.uuid4()
    assert client.get(f"/api/library/{fake_id}").status_code == 403
    assert client.request("DELETE", f"/api/library/{fake_id}", json={"confirm": True}).status_code == 403


def test_import_single_text_file_via_api(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "hello.txt", b"Detta ar ett testdokument.")
    assert job["status"] == "completed"
    assert job["succeeded_count"] == 1

    listed = client.get("/api/library")
    assert listed.status_code == 200
    titles = [d["title"] for d in listed.json()]
    assert "hello.txt" in titles


def test_import_zip_package_via_api(client):
    csrf = _login(client)
    raw = _make_zip({"a.txt": b"Innehall A", "b.md": b"# Innehall B"})
    res = client.post("/api/library/import", files={"file": ("package.zip", raw, "application/zip")}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    job = _wait_for_job(client, res.json()["id"])
    assert job["status"] == "completed"
    assert job["succeeded_count"] == 2

    listed = client.get("/api/library").json()
    assert len(listed) >= 2


def test_reimporting_identical_zip_returns_the_same_completed_job_not_a_new_one(client):
    csrf = _login(client)
    raw = _make_zip({"a.txt": b"identiskt innehall"})
    res1 = client.post("/api/library/import", files={"file": ("p.zip", raw, "application/zip")}, headers={"X-CSRF-Token": csrf})
    job1 = _wait_for_job(client, res1.json()["id"])

    res2 = client.post("/api/library/import", files={"file": ("p-again.zip", raw, "application/zip")}, headers={"X-CSRF-Token": csrf})
    job2 = res2.json()  # returned synchronously — no new job was scheduled at all

    assert job1["id"] == job2["id"]
    assert job2["status"] == "completed"


def test_get_job_status(client):
    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("j.txt", b"jobbstatus-test", "text/plain")}, headers={"X-CSRF-Token": csrf})
    job = _wait_for_job(client, res.json()["id"])
    assert job["status"] == "completed"


def test_source_detail_includes_versions_and_chunk_preview(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "detail.txt", b"Text som ska bli en chunk-forhandsvisning.")
    source_id = job["file_results"][0]["source_id"]

    detail = client.get(f"/api/library/{source_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["versions"]) == 1
    assert len(body["chunk_preview"]) >= 1


def test_source_detail_includes_extracted_claims_with_computed_confidence(client, monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _claim_chat(self, messages, model, **kwargs):
        return ChatResult(content='["Bolaget grundades 2019 i Stockholm."]', provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    monkeypatch.setattr(OpenAIProvider, "chat", _claim_chat)

    csrf = _login(client)
    job = _import_and_wait(client, csrf, "claims.txt", b"Bolaget grundades 2019 i Stockholm.")
    source_id = job["file_results"][0]["source_id"]

    detail = client.get(f"/api/library/{source_id}")
    assert detail.status_code == 200
    claims = detail.json()["claims"]
    assert len(claims) == 1
    assert claims[0]["claim_text"] == "Bolaget grundades 2019 i Stockholm."
    assert claims[0]["confidence"] == "likely"  # well-grounded, but not "certain" without independent corroboration
    assert claims[0]["status"] == "active"


def test_delete_requires_explicit_confirmation(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "delete-me.txt", b"raderas snart")
    source_id = job["file_results"][0]["source_id"]

    unconfirmed = client.request("DELETE", f"/api/library/{source_id}", json={"confirm": False}, headers={"X-CSRF-Token": csrf})
    assert unconfirmed.status_code == 400
    assert client.get(f"/api/library/{source_id}").status_code == 200  # still there

    confirmed = client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
    assert confirmed.status_code == 200
    assert client.get(f"/api/library/{source_id}").status_code == 404  # gone from a normal read


def test_deleted_source_is_excluded_from_list_and_search(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "findme.txt", b"ett unikt sokbart uttryck harinne")
    source_id = job["file_results"][0]["source_id"]

    hits_before = client.get("/api/library/search/hybrid", params={"q": "unikt"}).json()
    assert any(h["document_id"] == source_id for h in hits_before)

    client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})

    listed = client.get("/api/library").json()
    assert all(d["id"] != source_id for d in listed)

    hits_after = client.get("/api/library/search/hybrid", params={"q": "unikt"}).json()
    assert all(h["document_id"] != source_id for h in hits_after)


def test_hybrid_search_finds_exact_text_match(client):
    csrf = _login(client)
    _import_and_wait(client, csrf, "searchable.txt", b"Det unika ordet zorbaflex finns bara har.")
    res = client.get("/api/library/search/hybrid", params={"q": "zorbaflex"})
    assert res.status_code == 200
    hits = res.json()
    assert len(hits) >= 1
    assert hits[0]["text_match"] is True


def test_create_and_read_source_relationship(client):
    csrf = _login(client)
    job_a = _import_and_wait(client, csrf, "old.txt", b"gammalt beslut")
    job_b = _import_and_wait(client, csrf, "new.txt", b"nytt beslut")
    a = job_a["file_results"][0]["source_id"]
    b = job_b["file_results"][0]["source_id"]

    res = client.post(
        f"/api/library/{b}/relationships",
        json={"to_source_id": a, "relationship_type": "supersedes", "note": "Ersätter det gamla beslutet."},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text

    detail = client.get(f"/api/library/{a}").json()
    assert len(detail["relationships"]) == 1
    assert detail["relationships"][0]["relationship_type"] == "supersedes"


def test_relationship_to_nonexistent_source_is_404(client):
    import uuid

    csrf = _login(client)
    job = _import_and_wait(client, csrf, "solo.txt", b"ensam")
    a = job["file_results"][0]["source_id"]

    res = client.post(
        f"/api/library/{a}/relationships",
        json={"to_source_id": str(uuid.uuid4()), "relationship_type": "supports"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 404


def test_import_rejects_oversized_upload(client):
    csrf = _login(client)
    from app.routers.library import MAX_UPLOAD_BYTES

    huge = b"x" * (MAX_UPLOAD_BYTES + 1)
    res = client.post("/api/library/import", files={"file": ("huge.txt", huge, "text/plain")}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 413


def test_import_zip_security_violation_returns_failed_job_not_500(client):
    csrf = _login(client)
    raw = _make_zip({"../../etc/passwd": b"pwned"})
    res = client.post("/api/library/import", files={"file": ("evil.zip", raw, "application/zip")}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200  # the *request* succeeds — the *job* records the failure
    job = _wait_for_job(client, res.json()["id"])
    assert job["status"] == "failed"
    assert job["failure_reason"] is not None


def test_list_filters_by_classification_and_project(client):
    csrf = _login(client)
    manifest = b'{"documents": [{"file": "sec.txt", "classification": "security"}]}'
    raw = _make_zip({"manifest.json": manifest, "sec.txt": b"sakerhetsinnehall"})
    res1 = client.post("/api/library/import", files={"file": ("p.zip", raw, "application/zip")}, headers={"X-CSRF-Token": csrf})
    _wait_for_job(client, res1.json()["id"])
    _import_and_wait(client, csrf, "general.txt", b"allmant")

    security_only = client.get("/api/library", params={"classification": "security"}).json()
    assert all(d["classification"] == "security" for d in security_only)
    assert any(d["original_filename"] == "sec.txt" for d in security_only)


def test_a_source_deleted_via_library_disappears_from_the_older_documents_router_too(client):
    """Regression test: Document is the same underlying table both /api/documents (the
    original, simpler upload flow) and /api/library (Founder Knowledge Studio) operate on.
    Found as a real bug during E2E testing, not assumed: deleting a source through the
    library's soft-delete (deleted_at) left it still visible via GET /api/documents, which
    had no deleted_at filter at all. Both surfaces must agree on what's still there."""
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "shared-table.txt", b"innehall som finns i bada vyerna")
    source_id = job["file_results"][0]["source_id"]

    assert any(d["id"] == source_id for d in client.get("/api/documents").json())
    assert any(d["id"] == source_id for d in client.get("/api/library").json())

    client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})

    assert not any(d["id"] == source_id for d in client.get("/api/documents").json())
    assert not any(d["id"] == source_id for d in client.get("/api/library").json())


# --- STEG 12: secure URL-import model (POST /api/library/import-url) — records intent
# only, never fetches. See app/models/media_url_import.py's docstring. ---


def test_create_media_url_import_records_intent_and_never_advances_past_pending_review(client):
    csrf = _login(client)
    res = client.post(
        "/api/library/import-url",
        json={
            "url": "https://www.youtube.com/watch?v=abc123",
            "platform": "youtube",
            "consent_confirmed": True,
            "rights_note": "Mitt eget inspelade grundarsamtal.",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "pending_review"
    assert body["url"] == "https://www.youtube.com/watch?v=abc123"
    assert body["consent_confirmed"] is True

    listed = client.get("/api/library/url-imports").json()
    assert any(r["id"] == body["id"] and r["status"] == "pending_review" for r in listed)


def test_media_url_import_rejects_non_http_scheme(client):
    csrf = _login(client)
    res = client.post(
        "/api/library/import-url",
        json={"url": "file:///etc/passwd", "platform": "generic"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422


def test_media_url_import_rejects_unknown_platform(client):
    csrf = _login(client)
    res = client.post(
        "/api/library/import-url",
        json={"url": "https://example.com/video", "platform": "some-random-site"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422


def test_media_url_import_defaults_consent_to_false_when_not_provided(client):
    csrf = _login(client)
    res = client.post(
        "/api/library/import-url",
        json={"url": "https://vimeo.com/12345", "platform": "vimeo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    assert res.json()["consent_confirmed"] is False
