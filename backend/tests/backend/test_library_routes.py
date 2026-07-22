"""API-level tests for app/routers/library.py — Founder Knowledge Studio v1. Drives the
real HTTP surface (TestClient), not the orchestrator directly (see test_library_import.py
for that), so route wiring, auth/CSRF, request validation and response shaping are all
exercised together, the same way a real client would hit them."""

import asyncio
import io
import time
import uuid
import zipfile

import pytest

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _run_worker_once() -> bool:
    """Durable-worker package: POST /api/library/import no longer schedules a FastAPI
    BackgroundTask — it only writes the original file and creates a pending ImportJob (see
    app/routers/library.py's import_package). A real worker process (app/worker.py) picks
    pending jobs up independently via its own poll loop, but no such process runs during
    pytest, so tests must explicitly simulate one poll cycle. This runs the exact same
    Worker.run_once() a production worker runs: claim via Postgres FOR UPDATE SKIP LOCKED,
    then process to completion (including any in-process retries). Returns False if nothing
    was pending to claim."""
    from app.worker import Worker

    return asyncio.run(Worker().run_once())


def _wait_for_job(client, job_id: str, *, timeout: float = 5.0) -> dict:
    """Drives worker poll cycles synchronously (see _run_worker_once) instead of relying on
    background-task completion. One cycle already fully resolves a job to a terminal status
    in practice (process_claimed_job retries in-process), but the timeout loop is kept as a
    hang-guard rather than assuming that's always true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _run_worker_once()
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


def test_reimporting_identical_content_after_deleting_the_source_is_a_real_new_import(client):
    """Found during STEG 14's full vertical review, and the exact cause of a reproducible
    (not flaky) E2E failure: the whole-upload idempotency check above
    (test_reimporting_identical_zip_returns_the_same_completed_job_not_a_new_one) matched
    purely on checksum + status=="completed", with no regard for whether that job's document
    still exists. A founder who deletes a source and later re-imports the exact same file got
    back the OLD job object — status "completed", succeeded_count populated — while the
    library stayed permanently empty for that content, since no new Document was ever
    created and the old one was gone. This proves the fix in app/routers/library.py's
    import_package: a job is only treated as a duplicate if it still has a live (non-deleted)
    result."""
    csrf = _login(client)
    job1 = _import_and_wait(client, csrf, "reimport-after-delete.txt", b"innehall som raderas och importeras igen")
    source_id_1 = job1["file_results"][0]["source_id"]

    client.request("DELETE", f"/api/library/{source_id_1}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
    assert client.get(f"/api/library/{source_id_1}").status_code == 404

    job2 = _import_and_wait(client, csrf, "reimport-after-delete.txt", b"innehall som raderas och importeras igen")
    assert job2["id"] != job1["id"], "re-import after delete must be a real new job, not the stale deleted one"
    assert job2["status"] == "completed"
    assert job2["succeeded_count"] == 1
    source_id_2 = job2["file_results"][0]["source_id"]
    assert source_id_2 != source_id_1

    listed = client.get("/api/library").json()
    assert any(d["id"] == source_id_2 for d in listed)


def test_get_job_status(client):
    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("j.txt", b"jobbstatus-test", "text/plain")}, headers={"X-CSRF-Token": csrf})
    job = _wait_for_job(client, res.json()["id"])
    assert job["status"] == "completed"


# --- Life Library upload consolidation package: server-recoverable job status (DEL 3) ---


def test_list_jobs_requires_founder(client, make_verified_user):
    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    assert client.get("/api/library/jobs").status_code == 403


def test_list_jobs_returns_the_founders_recent_jobs_newest_first(client):
    csrf = _login(client)
    _import_and_wait(client, csrf, "job-list-a.txt", b"innehall a")
    _import_and_wait(client, csrf, "job-list-b.txt", b"innehall b")

    res = client.get("/api/library/jobs")
    assert res.status_code == 200
    jobs = res.json()
    filenames = [j["source_filename"] for j in jobs]
    assert "job-list-a.txt" in filenames
    assert "job-list-b.txt" in filenames
    assert filenames.index("job-list-b.txt") < filenames.index("job-list-a.txt")  # newest first


def test_list_jobs_lets_the_ui_recover_a_still_running_job_after_reload(client, superuser_db):
    """The UI must be able to recover server job state after a page reload/fresh login and
    must never show 'completed' before the server actually says so — this endpoint is what
    makes that possible: an in-flight (pending/running) job is listed with its real status,
    not silently dropped. A real background import always finishes synchronously within
    TestClient's request/response cycle (see _wait_for_job's own docstring), so a still-
    running job is set up directly here rather than relying on catching one mid-flight."""
    from app.models.import_job import ImportJob, ImportJobStatus

    _login(client)
    owner_id = client.get("/api/auth/me").json()["id"]

    running_job = ImportJob(
        owner_id=owner_id, status=ImportJobStatus.running, source_filename="slow.txt", source_checksum="a" * 64
    )
    superuser_db.add(running_job)
    superuser_db.commit()

    jobs = client.get("/api/library/jobs").json()
    match = next(j for j in jobs if j["source_filename"] == "slow.txt")
    assert match["status"] == "running"


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


def test_deleting_a_source_still_mid_pipeline_leaves_it_in_a_definitive_terminal_state(client, superuser_db):
    """DEL 5's 'väntande/pågående jobb får inte lämnas i ett odefinierat tillstånd': a
    founder can delete a source whose background indexing hasn't reached a terminal
    IndexStatus yet (a narrow but real race between DEL 4's earlier document creation and
    the async extraction/embedding steps still running). Simulates that race directly
    rather than relying on real timing: after a normal completed import, the document's
    status is rewound to a non-terminal one, then deleted — the row must come out the other
    side with a real terminal status, not frozen mid-pipeline forever. Durable-worker
    package: a source deleted mid-pipeline is a cancellation, not a failure — the file/job
    were never broken, the founder just chose to stop them — so the terminal status is now
    IndexStatus.cancelled, not .failed (see app/routers/library.py's delete_source)."""
    import uuid as uuid_mod

    from app.models.document import Document, IndexStatus

    csrf = _login(client)
    job = _import_and_wait(client, csrf, "mid-pipeline.txt", b"innehall som hinner bli klart forst")
    source_id = job["file_results"][0]["source_id"]

    doc = superuser_db.get(Document, uuid_mod.UUID(source_id))
    doc.status = IndexStatus.embedding
    doc.error_message = None
    superuser_db.add(doc)
    superuser_db.commit()

    res = client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200

    superuser_db.expire_all()
    doc2 = superuser_db.get(Document, uuid_mod.UUID(source_id))
    assert doc2.deleted_at is not None
    assert doc2.status == IndexStatus.cancelled
    assert doc2.error_message is not None


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


# --- STEG 13: multimedia in UI — GET /api/library/{id}/media, and the source detail
# route's full timestamped segment list ---

VALID_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def test_media_source_serves_playable_bytes_via_media_endpoint(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "clip.mp3", VALID_MP3, "audio/mpeg")
    assert job["status"] == "completed"
    source_id = job["file_results"][0]["source_id"]

    res = client.get(f"/api/library/{source_id}/media")
    assert res.status_code == 200
    assert res.content == VALID_MP3
    assert res.headers["content-type"] == "audio/mpeg"


def test_media_endpoint_404s_for_a_text_source(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "notes.txt", b"vanlig text, inget ljud har")
    source_id = job["file_results"][0]["source_id"]

    res = client.get(f"/api/library/{source_id}/media")
    assert res.status_code == 404


def test_media_endpoint_404s_for_an_unknown_source(client):
    _login(client)
    res = client.get(f"/api/library/{uuid.uuid4()}/media")
    assert res.status_code == 404


def test_source_detail_includes_full_timestamped_segment_list_for_a_media_source(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "talk.mp3", VALID_MP3, "audio/mpeg")
    source_id = job["file_results"][0]["source_id"]

    detail = client.get(f"/api/library/{source_id}").json()
    assert len(detail["segments"]) >= 1
    assert detail["segments"][0]["start_seconds"] == 0.0
    assert detail["media_duration_seconds"] is not None
    assert detail["transcript_provider"] == "mock"


def test_source_detail_segments_stay_empty_for_a_text_source(client):
    csrf = _login(client)
    job = _import_and_wait(client, csrf, "plain.txt", b"vanligt textinnehall utan ljud")
    source_id = job["file_results"][0]["source_id"]

    detail = client.get(f"/api/library/{source_id}").json()
    assert detail["segments"] == []
    assert detail["media_duration_seconds"] is None
