"""Shared-original retrieval — Founder endpoint (GET /api/library/{id}/original) + Life's
authorized service path (app/library/originals.py). Full required matrix (Blueprint §16.6):

  * byte-identical original download            * missing / corrupted storage object
  * sha-256 integrity                           * safe filename + Content-Disposition
  * owner authorization                         * concurrent reads
  * cross-owner denial                          * no unauthorized external egress
  * revoked / deleted source denial             * no mutation of the original

HTTP-level tests drive the real route through TestClient (auth, worker storage, download) the
way a real client would; service-level tests cover the denial/integrity/concurrency/egress
cases directly against `load_original` / `load_original_as_life`, which is where all the
authorization and integrity logic actually lives.
"""

import hashlib
import importlib.util
import io
import threading
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.library.originals import (
    OriginalIntegrityError,
    OriginalNotAvailable,
    OriginalNotFound,
    load_original,
    load_original_as_life,
    safe_filename,
)
from app.models.document import Document, IndexStatus
from app.models.user import User
from app.storage import get_storage

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"

_APPLY_RUNTIME_PRIVILEGES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"
)


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """Same rationale as tests/backend/rag/test_library_routes.py: the import path exercised
    here reaches storage_key_still_referenced_global(), which mainai_app is only granted
    EXECUTE on via the shared privilege policy, never by conftest's blanket table GRANTs."""
    from app.config import get_settings

    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


# --- HTTP helpers (mirroring test_library_routes.py) ---------------------------------------


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _import(client, csrf: str, filename: str, content: bytes, content_type: str = "text/plain") -> str:
    """Upload one file and drive worker poll cycles until its source row exists; return
    source_id. We only need the ORIGINAL stored (set at original_stored, before any model), so
    this tolerates a job that parks at awaiting_provider when no embedding model is available."""
    import asyncio

    from app.worker import Worker

    res = client.post(
        "/api/library/import",
        files={"file": (filename, content, content_type)},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code in (200, 201), res.text
    job_id = res.json()["id"]
    for _ in range(40):
        job = client.get(f"/api/library/jobs/{job_id}").json()
        results = job.get("file_results") or []
        if results and results[0].get("source_id"):
            return results[0]["source_id"]
        if not asyncio.run(Worker().run_once()):
            # nothing left to claim; re-check once more below
            job = client.get(f"/api/library/jobs/{job_id}").json()
            results = job.get("file_results") or []
            if results and results[0].get("source_id"):
                return results[0]["source_id"]
    raise AssertionError(f"import job {job_id} never produced a source_id")


# --- service-level setup helpers -----------------------------------------------------------


def _store_blob(content: bytes):
    reader = io.BytesIO(content)
    return get_storage().write_stream(lambda: reader.read(1 << 20), max_bytes=100 * 1024 * 1024)


def _make_document(superuser_db, *, owner_id, content: bytes, filename="orig.txt", media_type="text/plain",
                   status=IndexStatus.indexed, with_checksum=True, store=True):
    blob = _store_blob(content) if store else None
    doc = Document(
        uploaded_by=owner_id,
        title=filename,
        status=status,
        original_filename=filename,
        media_type=media_type,
        storage_key=blob.storage_key if blob else None,
        checksum=(blob.sha256 if (blob and with_checksum) else None),
        size_bytes=(blob.size_bytes if blob else None),
        stored_at=datetime.utcnow() if blob else None,
    )
    superuser_db.add(doc)
    superuser_db.commit()
    return doc.id


def _as_owner(db, owner_id):
    """Scope a restricted (mainai_app) session to this owner so RLS exposes their rows."""
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


# ============================ HTTP-level (real route) ======================================


def test_founder_download_is_byte_identical_and_sha256_matches(client):
    csrf = _login(client)
    raw = "Grundarens original — bevaras byte-exakt. Räksmörgås. åäö.\n".encode("utf-8")
    source_id = _import(client, csrf, "grundare.txt", raw)

    res = client.get(f"/api/library/{source_id}/original")
    assert res.status_code == 200, res.text
    assert res.content == raw  # byte-identical
    assert hashlib.sha256(res.content).hexdigest() == hashlib.sha256(raw).hexdigest()  # sha-256 integrity
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.headers["x-content-type-options"] == "nosniff"
    cd = res.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert "grundare.txt" in cd


def test_download_forces_attachment_octet_stream_even_for_html(client):
    """An original that is text/html must never come back inline as html in the founder's
    authenticated session — always octet-stream + attachment."""
    csrf = _login(client)
    raw = b"<html><script>alert(1)</script><body>hej</body></html>"
    source_id = _import(client, csrf, "evil.html", raw, content_type="text/html")

    res = client.get(f"/api/library/{source_id}/original")
    assert res.status_code == 200
    assert res.content == raw
    assert res.headers["content-type"] == "application/octet-stream"
    assert "attachment;" in res.headers["content-disposition"]


def test_deleted_source_download_is_404(client):
    csrf = _login(client)
    raw = b"raderas sedan"
    source_id = _import(client, csrf, "to-delete.txt", raw)
    assert client.get(f"/api/library/{source_id}/original").status_code == 200

    client.request("DELETE", f"/api/library/{source_id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf})
    assert client.get(f"/api/library/{source_id}/original").status_code == 404


def test_unknown_source_download_is_404(client):
    _login(client)
    assert client.get(f"/api/library/{uuid.uuid4()}/original").status_code == 404


def test_download_requires_founder(client, make_verified_user):
    """A logged-in non-founder is denied, like every other /api/library route."""
    user, password = make_verified_user(role="member")
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    # a random id is enough: auth is checked before the handler ever runs
    res = client.get(f"/api/library/{uuid.uuid4()}/original")
    assert res.status_code in (401, 403)


def test_original_download_rejects_unauthenticated_session(client):
    """Authentication must fail before source lookup, even for a syntactically valid id."""
    client.cookies.clear()
    res = client.get(f"/api/library/{uuid.uuid4()}/original")
    assert res.status_code in (401, 403)


def test_concurrent_downloads_are_all_byte_identical(client):
    csrf = _login(client)
    raw = ("concurrent-läsning " * 500).encode("utf-8")
    source_id = _import(client, csrf, "concurrent.txt", raw)

    results: list[bytes] = []
    errors: list[Exception] = []

    def _fetch():
        try:
            r = client.get(f"/api/library/{source_id}/original")
            assert r.status_code == 200
            results.append(r.content)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_fetch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 8
    assert all(r == raw for r in results)


# ============================ service-level (denials / integrity / egress) =================


def test_cross_owner_retrieval_is_denied(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    attacker, _ = make_verified_user()
    raw = b"owner A hemligt original"
    source_id = _make_document(superuser_db, owner_id=owner.id, content=raw)

    # The attacker, scoped to their OWN id, cannot resolve owner A's source at all.
    _as_owner(db_session, attacker.id)
    with pytest.raises(OriginalNotFound):
        load_original(
            db_session, owner_id=attacker.id, source_id=source_id,
            actor_type="founder", actor_id=attacker.id, access_label="x",
        )

    # And Life scoped to the attacker is denied identically.
    _as_owner(db_session, attacker.id)
    with pytest.raises(OriginalNotFound):
        load_original_as_life(db_session, owner_id=attacker.id, source_id=source_id)


def test_owner_can_load_and_life_can_load_same_source(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    raw = b"delad atkomst: bade grundare och Life"
    source_id = _make_document(superuser_db, owner_id=owner.id, content=raw, filename="delad.txt")

    _as_owner(db_session, owner.id)
    founder_view = load_original(
        db_session, owner_id=owner.id, source_id=source_id,
        actor_type="founder", actor_id=owner.id, access_label="founder_download",
    )
    assert founder_view.content == raw
    assert founder_view.checksum == hashlib.sha256(raw).hexdigest()

    _as_owner(db_session, owner.id)
    life_view = load_original_as_life(db_session, owner_id=owner.id, source_id=source_id)
    assert life_view.content == raw  # same underlying capability, same bytes


def test_deleted_source_service_denied(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    source_id = _make_document(superuser_db, owner_id=owner.id, content=b"snart raderad")
    superuser_db.execute(
        sa_text("UPDATE documents SET deleted_at = now() WHERE id = :id"), {"id": str(source_id)}
    )
    superuser_db.commit()

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalNotFound):
        load_original_as_life(db_session, owner_id=owner.id, source_id=source_id)


def test_not_yet_stored_source_is_not_available(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    # A row still mid-upload: no storage_key yet.
    source_id = _make_document(
        superuser_db, owner_id=owner.id, content=b"irrelevant",
        status=IndexStatus.original_storing, store=False,
    )
    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalNotAvailable):
        load_original(
            db_session, owner_id=owner.id, source_id=source_id,
            actor_type="founder", actor_id=owner.id, access_label="x",
        )


def test_missing_storage_object_is_integrity_error(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    raw = b"original som forsvinner fran disk"
    source_id = _make_document(superuser_db, owner_id=owner.id, content=raw)

    # Physically remove the blob but keep the (checksum-bearing) DB row.
    doc = superuser_db.get(Document, source_id)
    get_storage().delete(doc.storage_key)

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalIntegrityError):
        load_original(
            db_session, owner_id=owner.id, source_id=source_id,
            actor_type="founder", actor_id=owner.id, access_label="x",
        )


def test_corrupted_checksum_is_integrity_error(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    raw = b"akta innehall"
    source_id = _make_document(superuser_db, owner_id=owner.id, content=raw)

    # Tamper with the recorded checksum so the stored bytes no longer match their anchor.
    superuser_db.execute(
        sa_text("UPDATE documents SET checksum = :c WHERE id = :id"),
        {"c": "0" * 64, "id": str(source_id)},
    )
    superuser_db.commit()

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalIntegrityError):
        load_original(
            db_session, owner_id=owner.id, source_id=source_id,
            actor_type="founder", actor_id=owner.id, access_label="x",
        )


def test_checksumless_legacy_original_uses_canonical_content_address_without_mutating_provenance(
    db_session, superuser_db, make_verified_user
):
    """A canonical server-generated storage key is itself a trustworthy sha256 anchor.

    Legacy rows can therefore be read only after their bytes verify against that address, but
    retrieval must not backfill the absent checksum and pretend historical provenance changed.
    """
    owner, _ = make_verified_user()
    raw = b"legacy bytes with a valid content-addressed identity"
    source_id = _make_document(
        superuser_db,
        owner_id=owner.id,
        content=raw,
        with_checksum=False,
    )
    before = superuser_db.get(Document, source_id)
    original_key = before.storage_key
    assert before.checksum is None

    _as_owner(db_session, owner.id)
    loaded = load_original(
        db_session,
        owner_id=owner.id,
        source_id=source_id,
        actor_type="founder",
        actor_id=owner.id,
        access_label="legacy-content-addressed",
    )

    assert loaded.content == raw
    assert loaded.checksum == hashlib.sha256(raw).hexdigest()
    superuser_db.expire_all()
    after = superuser_db.get(Document, source_id)
    assert after.checksum is None
    assert after.storage_key == original_key


def test_checksumless_legacy_original_without_canonical_anchor_fails_closed(
    db_session, superuser_db, make_verified_user
):
    """A regex-shaped but non-canonical database key is not a server-attested identity."""
    owner, _ = make_verified_user()
    raw = b"legacy bytes without a trustworthy address"
    digest = hashlib.sha256(raw).hexdigest()
    wrong_shard = "00" if not digest.startswith("00") else "ff"
    doc = Document(
        uploaded_by=owner.id,
        title="legacy unknown identity",
        status=IndexStatus.indexed,
        original_filename="legacy.txt",
        media_type="text/plain",
        storage_key=f"{wrong_shard}/{digest}",
        checksum=None,
        size_bytes=len(raw),
        stored_at=datetime.utcnow(),
    )
    superuser_db.add(doc)
    superuser_db.commit()

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalIntegrityError):
        load_original(
            db_session,
            owner_id=owner.id,
            source_id=doc.id,
            actor_type="founder",
            actor_id=owner.id,
            access_label="legacy-unknown-anchor",
        )


def test_checksumless_legacy_original_fails_closed(
    db_session, superuser_db, make_verified_user
):
    """Reproduce #346's unchecked-legacy risk with a valid key but changed physical bytes."""
    owner, _ = make_verified_user()
    raw = b"trusted legacy bytes before storage tampering"
    source_id = _make_document(
        superuser_db,
        owner_id=owner.id,
        content=raw,
        with_checksum=False,
    )
    doc = superuser_db.get(Document, source_id)
    storage = get_storage()
    # Test-only host tampering: keep the same length so size checks cannot catch it and the
    # content-address-derived hash must do the security work.
    replacement = b"X" * len(raw)
    storage_path = storage.root / doc.storage_key
    storage_path.write_bytes(replacement)

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalIntegrityError):
        load_original(
            db_session,
            owner_id=owner.id,
            source_id=source_id,
            actor_type="founder",
            actor_id=owner.id,
            access_label="legacy-tamper-probe",
        )

    superuser_db.expire_all()
    unchanged = superuser_db.get(Document, source_id)
    assert unchanged.checksum is None
    assert unchanged.storage_key == doc.storage_key
    assert storage_path.read_bytes() == replacement  # retrieval never silently repairs/mutates


def test_recorded_checksum_must_agree_with_content_address(
    db_session, superuser_db, make_verified_user
):
    """Conflicting provenance anchors fail closed even if either value looks well formed."""
    owner, _ = make_verified_user()
    source_id = _make_document(superuser_db, owner_id=owner.id, content=b"anchor A")
    superuser_db.execute(
        sa_text("UPDATE documents SET checksum = :checksum WHERE id = :id"),
        {"checksum": hashlib.sha256(b"anchor B").hexdigest(), "id": str(source_id)},
    )
    superuser_db.commit()

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalIntegrityError):
        load_original(
            db_session,
            owner_id=owner.id,
            source_id=source_id,
            actor_type="founder",
            actor_id=owner.id,
            access_label="conflicting-anchors",
        )


def test_database_storage_key_cannot_traverse_storage_root(db_session, superuser_db, make_verified_user):
    """A tampered database row must not turn the server-side storage key into a file read."""
    owner, _ = make_verified_user()
    raw = b"real original"
    doc = Document(
        uploaded_by=owner.id,
        title="tampered storage key",
        status=IndexStatus.indexed,
        original_filename="original.txt",
        media_type="text/plain",
        storage_key="../../etc/passwd",
        checksum=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        stored_at=datetime.utcnow(),
    )
    superuser_db.add(doc)
    superuser_db.commit()

    _as_owner(db_session, owner.id)
    with pytest.raises(OriginalIntegrityError):
        load_original(
            db_session,
            owner_id=owner.id,
            source_id=doc.id,
            actor_type="founder",
            actor_id=owner.id,
            access_label="path-probe",
        )


def test_http_content_disposition_rejects_header_injection(client, superuser_db):
    """Exercise the real response boundary, not only the pure filename sanitizer."""
    _login(client)
    founder = superuser_db.query(User).filter(User.email == FOUNDER_EMAIL).one()
    source_id = _make_document(
        superuser_db,
        owner_id=founder.id,
        content=b"header probe",
        filename='../../evil"\r\nSet-Cookie: owned=1.html',
        media_type="text/html",
    )

    res = client.get(f"/api/library/{source_id}/original")
    assert res.status_code == 200, res.text
    disposition = res.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert res.headers.get("set-cookie") is None
    assert "../" not in disposition and "..\\" not in disposition
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.headers["x-content-type-options"] == "nosniff"


def test_retrieval_performs_no_provider_call_or_egress(db_session, superuser_db, make_verified_user, monkeypatch):
    """Reading an original must never touch a provider / egress path."""
    import app.providers.registry as registry

    async def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("retrieval must not call any provider / egress path")

    monkeypatch.setattr(registry, "embed_with_policy", _boom)
    monkeypatch.setattr(registry, "chat_with_fallback", _boom)

    owner, _ = make_verified_user()
    raw = b"ingen egress vid lasning"
    source_id = _make_document(superuser_db, owner_id=owner.id, content=raw)

    _as_owner(db_session, owner.id)
    loaded = load_original_as_life(db_session, owner_id=owner.id, source_id=source_id)
    assert loaded.content == raw  # succeeded with providers rigged to explode


def test_retrieval_does_not_mutate_the_original(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    raw = b"original far aldrig muteras av en lasning"
    source_id = _make_document(superuser_db, owner_id=owner.id, content=raw)
    doc_before = superuser_db.get(Document, source_id)
    key, checksum = doc_before.storage_key, doc_before.checksum

    _as_owner(db_session, owner.id)
    load_original_as_life(db_session, owner_id=owner.id, source_id=source_id)

    superuser_db.expire_all()
    doc_after = superuser_db.get(Document, source_id)
    assert doc_after.storage_key == key
    assert doc_after.checksum == checksum
    assert doc_after.deleted_at is None
    # The physical blob is unchanged and still verifies.
    with get_storage().open_read(key) as h:
        assert hashlib.sha256(h.read()).hexdigest() == checksum


def test_retrieval_writes_an_audit_row(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    source_id = _make_document(superuser_db, owner_id=owner.id, content=b"revisionsspar")

    _as_owner(db_session, owner.id)
    load_original(
        db_session, owner_id=owner.id, source_id=source_id,
        actor_type="founder", actor_id=owner.id, access_label="founder_download",
    )

    row = superuser_db.execute(
        sa_text(
            "SELECT action, entity_id, detail FROM audit_log "
            "WHERE action = 'original_access' AND entity_id = :eid ORDER BY created_at DESC LIMIT 1"
        ),
        {"eid": str(source_id)},
    ).first()
    assert row is not None
    assert "actor_type=founder" in (row[2] or "")
    # The storage key must never leak into the audit detail.
    doc = superuser_db.get(Document, source_id)
    assert doc.storage_key not in (row[2] or "")


# --- pure unit: filename sanitizer ---------------------------------------------------------


def test_safe_filename_strips_paths_and_control_chars():
    sid = uuid.uuid4()
    assert safe_filename("../../etc/passwd", source_id=sid) == "passwd"  # directory stripped
    assert "/" not in safe_filename("a/b/c.txt", source_id=sid)
    assert "\\" not in safe_filename("a\\b\\c.txt", source_id=sid)
    assert safe_filename("", source_id=sid) == f"original-{sid}"
    assert safe_filename(None, source_id=sid) == f"original-{sid}"
    # a header-injection attempt collapses to safe characters only
    injected = safe_filename('x"\r\nSet-Cookie: a=b', source_id=sid)
    assert '"' not in injected and "\r" not in injected and "\n" not in injected
