"""API-level tests for app/routers/library.py — Founder Knowledge Studio v1. Drives the
real HTTP surface (TestClient), not the orchestrator directly (see tests/backend/rag/test_library_import.py
for that), so route wiring, auth/CSRF, request validation and response shaping are all
exercised together, the same way a real client would hit them."""

import asyncio
import importlib.util
import io
import time
import uuid
import zipfile
from pathlib import Path

import pytest

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """Pass 30: the empty-upload path now calls storage_key_still_referenced_global() (via
    app/storage/references.py's delete_if_unreferenced()), which mainai_app is only granted
    EXECUTE on via apply_runtime_privileges.py/ensure_app_role.py's shared privilege policy --
    never automatically by tests/conftest.py's _test_database fixture's own blanket table/
    sequence GRANT ALL (function EXECUTE grants are a separate privilege class Postgres
    doesn't cover with that). Same fixture, same rationale, as tests/backend/test_source_
    purge.py's identical one; this file never needed it before this pass, since the OLD
    empty-upload path called storage.delete() directly, bypassing this function entirely."""
    from app.config import get_settings

    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


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

    async def _fake_embed(self, texts, model, **kwargs):
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


def test_a_failed_upload_can_be_deleted_cleanly_and_repeated_delete_is_idempotent(client, superuser_db):
    """Founder bug report: a Document that reached the terminal IndexStatus.failed (an
    extraction or storage failure, not a mid-pipeline race — see the sibling test below for
    that case) must be deletable through the exact same DELETE /api/library/{id} route every
    other source uses. Investigated end-to-end (live backend + live frontend, not just this
    test) after a report that clicking "Ta bort" on a failed row appeared to do nothing —
    no bug was found in this exact request path (confirmed via a real Postgres-backed
    request here, and separately via a real browser click-through against a live server in
    this session); this test is the permanent regression guard for that investigation,
    covering the founder's own five acceptance criteria:
      1. a failed upload/import can be deleted
      3. the backend returns the correct status (200, then 404 for a re-delete)
      4. the source disappears from a normal Library read afterwards
      5. a repeated delete does not 500 — it 404s cleanly, exactly like deleting any other
         already-deleted or nonexistent source id does
    (Criterion 2 — the frontend sends the right request — is a UI-layer property this
    backend test can't exercise directly; it was verified separately via a live browser
    session in the same investigation, see the delivery report.)"""
    import uuid as uuid_mod

    from app.models.document import Document, IndexStatus

    csrf = _login(client)
    owner_id = uuid_mod.UUID(client.get("/api/auth/me").json()["id"])

    # A document that reached a genuine terminal failure — e.g. extraction failed after the
    # original file was already durably stored — rather than an in-progress row, matching
    # what the founder's real Library actually shows for a failed import.
    doc = Document(
        uploaded_by=owner_id,
        title="misslyckad-import.txt",
        original_filename="misslyckad-import.txt",
        checksum="c" * 64,
        status=IndexStatus.failed,
        error_message="Extraktion misslyckades (simulerat för test).",
        chunk_count=0,
    )
    superuser_db.add(doc)
    superuser_db.commit()
    source_id = str(doc.id)

    # 1 + 4: visible before deletion, through the same read path the Library UI uses.
    assert client.get(f"/api/library/{source_id}").status_code == 200

    # 3: the actual delete succeeds with a clean 200.
    res = client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "deleted"}

    # 4: gone from a normal read and from the list.
    assert client.get(f"/api/library/{source_id}").status_code == 404
    assert all(d["id"] != source_id for d in client.get("/api/library").json())

    # 5: repeating the exact same delete call must not 500 — a clean 404, not a crash.
    repeat = client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
    assert repeat.status_code == 404
    assert repeat.status_code != 500


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

    hits_before = client.get("/api/library/search/hybrid", params={"q": "unikt"}).json()["results"]
    assert any(h["document_id"] == source_id for h in hits_before)

    client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})

    listed = client.get("/api/library").json()
    assert all(d["id"] != source_id for d in listed)

    hits_after = client.get("/api/library/search/hybrid", params={"q": "unikt"}).json()["results"]
    assert all(h["document_id"] != source_id for h in hits_after)


def test_hybrid_search_finds_exact_text_match(client):
    csrf = _login(client)
    _import_and_wait(client, csrf, "searchable.txt", b"Det unika ordet zorbaflex finns bara har.")
    res = client.get("/api/library/search/hybrid", params={"q": "zorbaflex"})
    assert res.status_code == 200
    body = res.json()
    assert body["semantic_search_available"] is True
    assert body["degraded_reason"] is None
    hits = body["results"]
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


def test_concurrent_blob_deletion_during_upload_finalization_fails_closed_without_creating_a_job(
    client, superuser_db, monkeypatch
):
    """Pass 22, router-level companion to tests/backend/storage/test_source_purge.py's low-level
    advisory-lock proof (test_storage_key_lock_serializes_upload_and_purge_...): if the
    just-written blob vanishes between storage.write_stream() finishing and this request's own
    storage_key lock being acquired (e.g. a concurrent retry_source_blob_purge() call), the
    endpoint must fail closed -- no ImportJob committed referencing a blob that no longer
    exists -- rather than silently proceeding as if nothing happened."""
    from app.storage.local_fs import LocalFilesystemStorage

    csrf = _login(client)

    def _pretend_missing(self, storage_key):
        return False

    monkeypatch.setattr(LocalFilesystemStorage, "exists", _pretend_missing)
    res = client.post(
        "/api/library/import",
        files={"file": ("raced-away.txt", b"deleted by a concurrent purge", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )

    assert res.status_code == 409

    from app.models.import_job import ImportJob

    matching_jobs = superuser_db.query(ImportJob).filter_by(source_filename="raced-away.txt").all()
    assert matching_jobs == [], "no ImportJob may exist referencing a blob that was gone at commit time"


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


# --- Pass 30 (a fourth founder review round): empty-upload rejection must go through the SAME
# canonical check-then-act protocol as every other physical blob delete -- content-addressing
# means every empty upload shares the exact same storage_key, so an ungated delete here could
# destroy a completely unrelated, already-live reference sharing that key (see
# app/storage/references.py's delete_if_unreferenced() module docstring for the full incident).
# Tests A-D below are the founder's own lettering; E/F (the race and StorageError cases) live in
# tests/backend/storage/test_source_purge.py's Pass 30 section, at the references.py function level
# rather than through the full HTTP stack.


def _empty_storage_key() -> str:
    from app.storage import get_storage

    blob = get_storage().write_stream(lambda: b"", max_bytes=1)
    assert blob.size_bytes == 0
    return blob.storage_key


def test_empty_upload_does_not_delete_a_blob_still_referenced_by_a_project_source(client, superuser_db):
    """Test A (founder's lettering)."""
    from app.models.project_memory import ProjectSource
    from app.storage import get_storage

    storage_key = _empty_storage_key()
    project_source = ProjectSource(source_type="doc", source_ref="docs/EMPTY.md", storage_key=storage_key, ingested_by="test")
    superuser_db.add(project_source)
    superuser_db.commit()
    project_source_id = project_source.id

    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("empty.txt", b"", "text/plain")}, headers={"X-CSRF-Token": csrf})

    assert res.status_code == 400
    assert get_storage().exists(storage_key)  # untouched -- Project Memory still needs it

    superuser_db.expire_all()
    still_there = superuser_db.query(ProjectSource).filter_by(id=project_source_id).one()
    assert still_there.storage_key == storage_key


def test_empty_upload_does_not_delete_a_blob_still_referenced_by_a_project_checkpoint(client, superuser_db):
    """Test B (founder's lettering)."""
    from app.models.project_memory import ProjectCheckpoint
    from app.storage import get_storage

    storage_key = _empty_storage_key()
    checkpoint = ProjectCheckpoint(
        summary="empty-upload test", branch_name="main", open_pr_refs="", brief_storage_key=storage_key, brief_sha256="a" * 64, created_by="test"
    )
    superuser_db.add(checkpoint)
    superuser_db.commit()
    checkpoint_id = checkpoint.id

    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("empty.txt", b"", "text/plain")}, headers={"X-CSRF-Token": csrf})

    assert res.status_code == 400
    assert get_storage().exists(storage_key)

    superuser_db.expire_all()
    still_there = superuser_db.query(ProjectCheckpoint).filter_by(id=checkpoint_id).one()
    assert still_there.brief_storage_key == storage_key


def test_empty_upload_does_not_delete_a_blob_still_referenced_by_another_owners_document(client, superuser_db, make_verified_user):
    """Test C (founder's lettering): a DIFFERENT founder-role owner's live Document sharing
    the same (empty-content) storage_key must survive an unrelated empty upload."""
    from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus
    from app.storage import get_storage

    other_owner, _ = make_verified_user(role="founder")
    storage_key = _empty_storage_key()
    other_document = Document(
        title="other owner's document",
        source=DocumentSource.upload,
        uploaded_by=other_owner.id,
        active_truth_status=ActiveTruthStatus.active,
        status=IndexStatus.indexed,
        storage_key=storage_key,
    )
    superuser_db.add(other_document)
    superuser_db.commit()
    other_document_id = other_document.id

    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("empty.txt", b"", "text/plain")}, headers={"X-CSRF-Token": csrf})

    assert res.status_code == 400
    assert get_storage().exists(storage_key)

    superuser_db.expire_all()
    still_there = superuser_db.query(Document).filter_by(id=other_document_id).one()
    assert still_there.storage_key == storage_key


def test_empty_upload_purges_a_genuinely_unreferenced_empty_blob(client):
    """Test D (founder's lettering): with NOTHING in any domain referencing the empty-content
    key, the endpoint must still correctly purge it -- the fix must not turn into "never
    delete empty blobs at all", only "never delete one that's still needed"."""
    from app.storage import get_storage

    storage_key = _empty_storage_key()
    assert get_storage().exists(storage_key)  # test setup: the blob genuinely exists first

    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("empty.txt", b"", "text/plain")}, headers={"X-CSRF-Token": csrf})

    assert res.status_code == 400
    assert not get_storage().exists(storage_key)


def test_empty_upload_never_creates_an_import_job(client, superuser_db):
    """No ImportJob (or Document) row is ever created for a rejected empty upload -- the
    early-return happens before any DB row is written."""
    from app.models.import_job import ImportJob

    before = superuser_db.query(ImportJob).count()
    csrf = _login(client)
    res = client.post("/api/library/import", files={"file": ("empty.txt", b"", "text/plain")}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 400

    superuser_db.expire_all()
    assert superuser_db.query(ImportJob).count() == before


# --- Pass 32 blocker 1 (an eighth founder review round): storage_orphan_risk in ops-status ---
#
# _record_storage_orphan_risk_audit() (app/storage/references.py) writes a durable AuditLog
# row, but nothing ever read it back -- founder ops-status still only showed worker/queue
# health, with zero signal that a blob may need a manual storage sweep. GET /api/library/
# ops/status now also surfaces get_storage_cleanup_ops_status()'s aggregated view. Test A
# (failed_not_queued creates the audit row in the first place) is covered end to end in
# tests/backend/storage/test_source_purge.py's test_delete_if_unreferenced_surfaces_a_double_failure_as_a_critical_
# log_and_an_audit_row -- the tests below focus on what THIS blocker actually required: that
# ops-status reads it back correctly.


def test_ops_status_shows_degraded_when_a_storage_orphan_risk_audit_row_exists(client, superuser_db):
    """Test B (founder's Pass 32 blocker-1 lettering)."""
    from datetime import datetime

    from app.models.audit import AuditLog

    superuser_db.add(
        AuditLog(
            user_id=None,
            action="storage_orphan_risk",
            entity_type="storage_key",
            entity_id="ab" * 32,
            detail="pass 32 blocker 1 test B: simulated orphan risk",
        )
    )
    superuser_db.commit()

    _login(client)
    res = client.get("/api/library/ops/status")
    assert res.status_code == 200
    body = res.json()

    assert body["storage_cleanup_degraded"] is True
    assert body["storage_orphan_risk_count"] >= 1
    assert body["latest_storage_orphan_risk_at"] is not None
    # Sanity: the timestamp really is recent, not some stale/default value.
    latest = datetime.fromisoformat(body["latest_storage_orphan_risk_at"].replace("Z", "+00:00"))
    assert (datetime.now(latest.tzinfo) - latest).total_seconds() < 60


def test_ops_status_never_exposes_a_raw_storage_key(client, superuser_db):
    """Test C (founder's Pass 32 blocker-1 lettering): ops-status must show that manual action
    may be needed WITHOUT ever leaking the actual storage_key -- OpsStatusOut only has counts/
    timestamps/booleans, never an entity_id/storage_key field."""
    from app.models.audit import AuditLog

    secret_key = "cd" * 32
    superuser_db.add(
        AuditLog(
            user_id=None,
            action="storage_orphan_risk",
            entity_type="storage_key",
            entity_id=secret_key,
            detail=f"pass 32 blocker 1 test C: entity_id={secret_key}",
        )
    )
    superuser_db.commit()

    _login(client)
    res = client.get("/api/library/ops/status")
    assert res.status_code == 200
    assert secret_key not in res.text
    assert set(res.json().keys()).isdisjoint({"storage_key", "entity_id", "storage_orphan_risk_keys"})


def test_ops_status_shows_not_degraded_under_normal_operation(client):
    """Test D (founder's Pass 32 blocker-1 lettering): a clean system (no orphan-risk audit
    rows, no failed storage-cleanup tasks) must report degraded=False -- this is the ordinary,
    expected state, not something a founder should ever have to double-check."""
    _login(client)
    res = client.get("/api/library/ops/status")
    assert res.status_code == 200
    body = res.json()

    assert body["storage_cleanup_degraded"] is False
    assert body["storage_orphan_risk_count"] == 0
    assert body["latest_storage_orphan_risk_at"] is None
    assert body["failed_storage_cleanup_tasks"] == 0
    assert body["oldest_failed_storage_cleanup_age_seconds"] is None


def test_ops_status_counts_pending_and_failed_storage_cleanup_tasks_correctly(client, superuser_db):
    """Test E (founder's Pass 32 blocker-1 lettering): pending/processing tasks are counted
    separately from failed ones, and a pending/processing-only backlog does NOT itself flip
    storage_cleanup_degraded -- see get_storage_cleanup_ops_status()'s own docstring for why
    (an enqueued durable retry waiting for the worker's next poll is normal operation)."""
    from sqlalchemy import text as sa_text

    for status in ("pending", "pending", "processing"):
        superuser_db.execute(
            sa_text(
                "INSERT INTO storage_deletion_tasks (id, operation_id, storage_key, status) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), :key, :status)"
            ),
            {"key": f"pass32-b1-teste-{status}-{uuid.uuid4().hex}", "status": status},
        )
    for _ in range(3):
        superuser_db.execute(
            sa_text(
                "INSERT INTO storage_deletion_tasks (id, operation_id, storage_key, status) "
                "VALUES (gen_random_uuid(), gen_random_uuid(), :key, 'failed')"
            ),
            {"key": f"pass32-b1-teste-failed-{uuid.uuid4().hex}"},
        )
    superuser_db.commit()

    _login(client)
    res = client.get("/api/library/ops/status")
    assert res.status_code == 200
    body = res.json()

    assert body["pending_storage_cleanup_tasks"] == 3  # 2 pending + 1 processing
    assert body["failed_storage_cleanup_tasks"] == 3
    assert body["oldest_failed_storage_cleanup_age_seconds"] is not None
    assert body["oldest_failed_storage_cleanup_age_seconds"] >= 0
    assert body["storage_cleanup_degraded"] is True  # driven by the failed tasks, not the pending ones


def test_ops_status_degraded_flag_resolves_for_tasks_but_not_for_audit_log_risk(client, superuser_db):
    """Test F (founder's Pass 32 blocker-1 lettering): the two halves of `storage_cleanup_
    degraded` behave differently by design -- a failed storage_deletion_tasks row genuinely
    self-resolves once the worker's retry succeeds (status moves to purged/retained_shared),
    but a storage_orphan_risk AuditLog row (audit_log is immutable/append-only, see that
    model's own docstring) currently has no resolution mechanism at all and stays degraded
    forever once it exists -- documented, deliberate, and proven here rather than silently
    assumed."""
    from sqlalchemy import text as sa_text

    _login(client)
    baseline = client.get("/api/library/ops/status").json()
    assert baseline["storage_cleanup_degraded"] is False

    task_key = f"pass32-b1-testf-{uuid.uuid4().hex}"
    superuser_db.execute(
        sa_text(
            "INSERT INTO storage_deletion_tasks (id, operation_id, storage_key, status) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), :key, 'failed')"
        ),
        {"key": task_key},
    )
    superuser_db.commit()

    degraded_from_task = client.get("/api/library/ops/status").json()
    assert degraded_from_task["storage_cleanup_degraded"] is True

    # The worker's retry loop succeeded -- this task is now resolved.
    superuser_db.execute(
        sa_text("UPDATE storage_deletion_tasks SET status = 'purged' WHERE storage_key = :key"),
        {"key": task_key},
    )
    superuser_db.commit()

    resolved = client.get("/api/library/ops/status").json()
    assert resolved["storage_cleanup_degraded"] is False, "a resolved (purged) task must stop counting as degraded"

    # Now the audit-log side: this one does NOT self-resolve, by design.
    from app.models.audit import AuditLog

    superuser_db.add(
        AuditLog(
            user_id=None,
            action="storage_orphan_risk",
            entity_type="storage_key",
            entity_id="ef" * 32,
            detail="pass 32 blocker 1 test F: audit-log risk never self-resolves",
        )
    )
    superuser_db.commit()

    degraded_from_audit = client.get("/api/library/ops/status").json()
    assert degraded_from_audit["storage_cleanup_degraded"] is True
    # No amount of task-table cleanup can clear this -- there is nothing to update; the audit
    # row itself is immutable. This is the documented, current limitation, not a bug.
