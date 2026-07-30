"""app/rag/source_purge.py::purge_source() — the shared purge service both
app/routers/library.py's delete_source and the older app/routers/documents.py's
delete_document call. Real local Postgres (RLS included), same pattern as
tests/backend/test_memory_source_units.py. HTTP-level "both routes share one service" checks
use the `client`/`superuser_db` fixtures, matching tests/backend/test_library_routes.py.
"""

import importlib.util
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models.document import ActiveTruthStatus, DeletionStatus, Document, DocumentSource, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import KnowledgeClaim
from app.models.memory_source_unit import (
    DocumentSourceUnit,
    LifecycleStatus,
    MemorySourceLifecycleEvent,
    MemorySourceUnit,
    SnapshotStatus,
)
from app.models.user import User, UserRole
from app.rag.memory_source import DocumentSourceLocator, get_or_create_memory_source_unit
from app.rag.source_purge import PURGE_REASON, SourcePurgeNotFoundError, purge_source, retry_source_blob_purge
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password
from app.storage import StorageError, get_storage

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "apply_runtime_privileges.py"


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """purge_source() calls transition_own_memory_source(), which mainai_app is only granted
    EXECUTE on via apply_runtime_privileges.py/ensure_app_role.py's shared privilege policy —
    never automatically by the test DB setup's blanket GRANT. Same fixture, same rationale, as
    tests/backend/test_memory_source_units.py's identical one: applied explicitly here rather
    than assuming whatever an earlier test module left behind, since test execution order is
    not guaranteed."""
    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="purge-owner@example.com", *, role=UserRole.founder) -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=role, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_document(session, owner_id, *, title="Källa", storage_key=None) -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
        status=IndexStatus.indexed,
        storage_key=storage_key,
    )
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value="Bolaget grundades 2019.") -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=0, text=text_value, embedding=[0.1] * 1536)
    session.add(chunk)
    session.commit()
    return chunk


def _make_claim(session, owner_id, document_id, memory_source_id, *, claim_text="Ett pastaende.") -> KnowledgeClaim:
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=document_id, claim_text=claim_text, extraction_version="v1", memory_source_id=memory_source_id
    )
    session.add(claim)
    session.commit()
    return claim


def _chunk_backed_msu(session, owner_id, document_id, chunk) -> uuid.UUID:
    _set_rls_user(session, owner_id)
    msu_id = get_or_create_memory_source_unit(
        session,
        DocumentSourceLocator(
            owner_id=owner_id,
            document_id=document_id,
            version_id=None,
            chunk_id=chunk.id,
            observed_at=datetime.now(timezone.utc),
            content_text=chunk.text,
            snapshot_status=SnapshotStatus.exact,
        ),
    )
    session.commit()
    return msu_id


def _store_real_blob(content: bytes) -> str:
    """Writes `content` through the real (test) storage backend, exactly like a real import
    would — needed for the phase-B blob tests below, which must observe an ACTUAL file on
    disk being deleted (or surviving), not just a DB row carrying a storage_key string that
    never corresponded to a real file."""
    pos = 0

    def _read():
        nonlocal pos
        chunk = content[pos : pos + (1 << 16)]
        pos += len(chunk)
        return chunk

    blob = get_storage().write_stream(_read, max_bytes=max(len(content), 1))
    return blob.storage_key


def _document_record_msu(session, owner_id, document_id) -> uuid.UUID:
    _set_rls_user(session, owner_id)
    msu_id = get_or_create_memory_source_unit(
        session,
        DocumentSourceLocator(
            owner_id=owner_id,
            document_id=document_id,
            version_id=None,
            chunk_id=None,
            observed_at=datetime.now(timezone.utc),
            content_text=None,
            snapshot_status=SnapshotStatus.missing,
        ),
    )
    session.commit()
    return msu_id


# --- 1-3: lifecycle transitions -------------------------------------------------------------


def test_purge_source_active_chunk_backed_source_purges_it():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.sources_purged == 1
        assert result.sources_already_purged == 0
        assert result.legacy_without_memory_source is False
        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.lifecycle_status == LifecycleStatus.purged
    finally:
        session.rollback()
        session.close()


def test_purge_source_revoked_source_purges_it():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'test revoke')"), {"id": str(msu_id)})
        session.commit()

        result = purge_source(session, document.id, owner.id)

        assert result.sources_purged == 1
        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.lifecycle_status == LifecycleStatus.purged
    finally:
        session.rollback()
        session.close()


def test_purge_source_already_purged_source_is_idempotent_noop():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'purged', 'pre-purged for test')"), {"id": str(msu_id)})
        session.commit()

        # purge_source must not attempt to re-transition an already-purged row (that would
        # raise "illegal transition purged -> purged") -- it recognizes it and no-ops.
        result = purge_source(session, document.id, owner.id)

        assert result.sources_purged == 0
        assert result.sources_already_purged == 1
        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.lifecycle_status == LifecycleStatus.purged
    finally:
        session.rollback()
        session.close()


# --- 4-5: multiple claims / multiple chunks+sources in one document -------------------------


def test_purge_source_multiple_claims_share_one_source_all_preserved():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        claim_a = _make_claim(session, owner.id, document.id, msu_id, claim_text="Claim A")
        claim_b = _make_claim(session, owner.id, document.id, msu_id, claim_text="Claim B")
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.sources_purged == 1
        assert result.claims_preserved == 2
        assert session.get(KnowledgeClaim, claim_a.id) is not None
        assert session.get(KnowledgeClaim, claim_b.id) is not None
        session.refresh(claim_a)
        session.refresh(claim_b)
        assert claim_a.memory_source_id == msu_id
        assert claim_b.memory_source_id == msu_id
    finally:
        session.rollback()
        session.close()


def test_purge_source_multiple_chunks_and_document_record_source_in_same_document():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk_a = _make_chunk(session, owner.id, document.id, text_value="Chunk A text.")
        chunk_b = _make_chunk(session, owner.id, document.id, text_value="Chunk B text.")
        msu_a = _chunk_backed_msu(session, owner.id, document.id, chunk_a)
        msu_b = _chunk_backed_msu(session, owner.id, document.id, chunk_b)
        msu_record = _document_record_msu(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.sources_purged == 3
        assert result.chunks_deleted == 2
        for msu_id in (msu_a, msu_b, msu_record):
            msu = session.get(MemorySourceUnit, msu_id)
            assert msu.lifecycle_status == LifecycleStatus.purged
        assert session.query(DocumentChunk).filter_by(document_id=document.id).count() == 0
    finally:
        session.rollback()
        session.close()


# --- 6-7: history survives, content cleared --------------------------------------------------


def test_purge_source_claims_and_lifecycle_events_survive():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        claim = _make_claim(session, owner.id, document.id, msu_id)
        _set_rls_user(session, owner.id)

        purge_source(session, document.id, owner.id)

        # MemorySourceUnit/DocumentSourceUnit/lifecycle-event rows are never hard-deleted by
        # source-level purge -- only erase_owner_memory() (full account erasure) can do that.
        assert session.get(MemorySourceUnit, msu_id) is not None
        assert session.get(DocumentSourceUnit, msu_id) is not None
        assert session.get(KnowledgeClaim, claim.id) is not None
        events = session.query(MemorySourceLifecycleEvent).filter_by(memory_source_id=msu_id).all()
        assert any(e.to_status == LifecycleStatus.purged and e.reason == PURGE_REASON for e in events)
    finally:
        session.rollback()
        session.close()


def test_purge_source_clears_content_text_hash_version():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id, text_value="Innehall som ska nollas.")
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        purge_source(session, document.id, owner.id)

        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.content_text is None
        assert msu.content_hash is None
        assert msu.content_hash_version is None
        assert msu.snapshot_status == SnapshotStatus.exact  # unchanged: "had an exact snapshot"
    finally:
        session.rollback()
        session.close()


# --- 8-9: chunk_id / lifecycle ordering -------------------------------------------------------


def test_purge_source_chunk_id_nulled_only_after_parent_purged():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        dsu_before = session.get(DocumentSourceUnit, msu_id)
        assert dsu_before.chunk_id == chunk.id

        purge_source(session, document.id, owner.id)

        session.expire_all()
        dsu_after = session.get(DocumentSourceUnit, msu_id)
        assert dsu_after.chunk_id is None  # cleared via ON DELETE SET NULL, legal now the parent is purged
    finally:
        session.rollback()
        session.close()


def test_deleting_chunk_before_purging_active_parent_is_rejected_by_trigger():
    """Regression/invariant proof for WHY purge_source() purges the MSU before deleting the
    chunk: skipping that ordering (as a naive delete would) must be rejected by
    trg_dsu_guard_update, not silently allowed."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        with pytest.raises((IntegrityError, DBAPIError), match="chunk_id cannot be cleared while parent is active"):
            session.query(DocumentChunk).filter_by(id=chunk.id).delete(synchronize_session=False)
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- 10-11: ownership / legacy ----------------------------------------------------------------


def test_purge_source_cross_owner_denied():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-a@example.com")
        owner_b = _make_user(session, email="purge-b@example.com")
        document_a = _make_document(session, owner_a.id)
        chunk = _make_chunk(session, owner_a.id, document_a.id)
        _chunk_backed_msu(session, owner_a.id, document_a.id, chunk)

        with pytest.raises(SourcePurgeNotFoundError):
            purge_source(session, document_a.id, owner_b.id)

        _set_rls_user(session, owner_a.id)
        msu = session.query(MemorySourceUnit).filter_by(owner_id=owner_a.id).first()
        assert msu.lifecycle_status == LifecycleStatus.active  # untouched
    finally:
        session.rollback()
        session.close()


def test_purge_source_legacy_document_without_memory_source_units():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        _make_chunk(session, owner.id, document.id)  # never backfilled/dual-written -- no MSU at all
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.legacy_without_memory_source is True
        assert result.sources_purged == 0
        assert result.chunks_deleted == 1
        session.refresh(document)
        assert document.deleted_at is not None
    finally:
        session.rollback()
        session.close()


# --- 12: atomicity -----------------------------------------------------------------------------


def test_purge_source_db_error_rolls_back_everything():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        real_commit = session.commit
        calls = {"n": 0}

        def _boom_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash before commit")
            return real_commit()

        session.commit = _boom_once
        try:
            with pytest.raises(RuntimeError, match="simulated crash"):
                purge_source(session, document.id, owner.id)
        finally:
            session.commit = real_commit

        session.rollback()
        session.expire_all()
        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.lifecycle_status == LifecycleStatus.active  # the purge transition never committed
        assert session.query(DocumentChunk).filter_by(id=chunk.id).count() == 1  # chunk delete never committed
        document_after = session.get(Document, document.id)
        assert document_after.deleted_at is None

        # a real retry afterward still works normally
        retry = purge_source(session, document.id, owner.id)
        assert retry.sources_purged == 1
        assert retry.chunks_deleted == 1
    finally:
        session.rollback()
        session.close()


# --- 13-14: blob handling -----------------------------------------------------------------------


def test_purge_source_shared_blob_not_deleted_while_other_document_references_it():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"shared blob content")
        document_a = _make_document(session, owner.id, title="A", storage_key=storage_key)
        document_b = _make_document(session, owner.id, title="B", storage_key=storage_key)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document_a.id, owner.id)

        assert result.deletion_status == DeletionStatus.pending  # still referenced by document_b
        session.refresh(document_b)
        assert document_b.deleted_at is None
        assert document_b.storage_key == storage_key
        assert get_storage().exists(storage_key)  # blob survives -- still referenced

        # After the LAST live reference is gone, the blob is actually removed.
        result_b = purge_source(session, document_b.id, owner.id)
        assert result_b.deletion_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key)

        # Explicitly exercising the retry function too, matching the founder's literal
        # requirement -- idempotent no-op once already purged.
        assert retry_source_blob_purge(session, document_a.id, owner.id) == DeletionStatus.purged
    finally:
        session.rollback()
        session.close()


def test_purge_source_blob_failure_leaves_retryable_deletion_status(monkeypatch):
    from app.storage.local_fs import LocalFilesystemStorage

    def _always_fails(self, storage_key):
        raise StorageError("disk full (simulerat)")

    monkeypatch.setattr(LocalFilesystemStorage, "delete", _always_fails)

    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"content that fails to delete")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.failed
        session.refresh(document)
        assert document.deletion_status == DeletionStatus.failed
        assert document.deleted_at is not None  # phase A (the DB purge) still succeeded
        assert get_storage().exists(storage_key)  # the file itself was never actually touched
    finally:
        session.rollback()
        session.close()


# --- Pass 21: two-phase blob purge (DB commit vs. physical file delete are NOT one atomic
# unit -- see app/rag/source_purge.py's module docstring for the bug this fixes) -----------


def test_phase_a_db_commit_failure_leaves_blob_untouched_and_never_calls_storage_delete():
    """The core Pass 21 regression: a DB commit failure during phase A must never have already
    physically deleted the blob -- proven here by asserting storage.delete() was never even
    called, not just that the file happens to still exist."""
    session = SessionLocal()
    try:
        from app.storage.local_fs import LocalFilesystemStorage

        owner = _make_user(session)
        storage_key = _store_real_blob(b"never actually deleted")
        document = _make_document(session, owner.id, storage_key=storage_key)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        delete_calls: list[str] = []
        real_delete = LocalFilesystemStorage.delete

        def _tracking_delete(self, key):
            delete_calls.append(key)
            return real_delete(self, key)

        real_commit = session.commit
        commit_calls = {"n": 0}

        def _boom_on_first_commit():
            commit_calls["n"] += 1
            if commit_calls["n"] == 1:
                raise RuntimeError("simulated phase A commit failure")
            return real_commit()

        LocalFilesystemStorage.delete = _tracking_delete
        session.commit = _boom_on_first_commit
        try:
            with pytest.raises(RuntimeError, match="simulated phase A commit failure"):
                purge_source(session, document.id, owner.id)
        finally:
            LocalFilesystemStorage.delete = real_delete
            session.commit = real_commit

        assert delete_calls == [], "storage.delete() must never be called before phase A commits"

        session.rollback()
        session.expire_all()
        assert get_storage().exists(storage_key)
        document_after = session.get(Document, document.id)
        assert document_after.deleted_at is None
        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.lifecycle_status == LifecycleStatus.active
        assert session.query(DocumentChunk).filter_by(id=chunk.id).count() == 1
    finally:
        session.rollback()
        session.close()


def test_retry_after_storage_failure_succeeds():
    session = SessionLocal()
    try:
        from app.storage.local_fs import LocalFilesystemStorage

        owner = _make_user(session)
        storage_key = _store_real_blob(b"retried after a transient failure")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _set_rls_user(session, owner.id)

        real_delete = LocalFilesystemStorage.delete

        def _always_fails(self, key):
            raise StorageError("disk full (simulerat, forsta forsoket)")

        LocalFilesystemStorage.delete = _always_fails
        try:
            first = purge_source(session, document.id, owner.id)
        finally:
            LocalFilesystemStorage.delete = real_delete

        assert first.deletion_status == DeletionStatus.failed
        assert get_storage().exists(storage_key)

        retried_status = retry_source_blob_purge(session, document.id, owner.id)

        assert retried_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key)
        session.refresh(document)
        assert document.deletion_status == DeletionStatus.purged
    finally:
        session.rollback()
        session.close()


def test_blob_deleted_but_status_commit_fails_next_retry_is_idempotent_and_reaches_purged():
    """The exact race the founder's review flagged, now proven safe: the physical unlink can
    succeed while the DB commit recording that fact fails. A later retry must not error on the
    already-missing file (Path.unlink(missing_ok=True)) and must still reach `purged`."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"deleted physically before status commit fails")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _set_rls_user(session, owner.id)

        # Simulate phase A having already committed (deleted_at set, deletion_status pending)
        # without going through purge_source's own chunk/MSU machinery -- irrelevant here.
        document.deleted_at = datetime.utcnow()
        document.deletion_status = DeletionStatus.pending
        session.add(document)
        session.commit()

        real_commit = session.commit
        commit_calls = {"n": 0}

        def _boom_on_first_commit():
            commit_calls["n"] += 1
            if commit_calls["n"] == 1:
                raise RuntimeError("simulated status-commit failure after a successful unlink")
            return real_commit()

        session.commit = _boom_on_first_commit
        try:
            with pytest.raises(RuntimeError, match="simulated status-commit failure"):
                retry_source_blob_purge(session, document.id, owner.id)
        finally:
            session.commit = real_commit

        # The physical delete already happened even though the status update didn't commit.
        assert not get_storage().exists(storage_key)

        session.rollback()
        session.expire_all()
        stuck = session.get(Document, document.id)
        assert stuck.deletion_status == DeletionStatus.pending  # unchanged -- still retryable

        # A fresh retry must not raise on the already-missing file, and must reach `purged`.
        final_status = retry_source_blob_purge(session, document.id, owner.id)
        assert final_status == DeletionStatus.purged
        session.refresh(document)
        assert document.deletion_status == DeletionStatus.purged
    finally:
        session.rollback()
        session.close()


# --- 15: both HTTP routes use the same service --------------------------------------------------


def test_both_http_routes_use_the_same_purge_service(client, superuser_db):
    import uuid as uuid_mod

    from app.models.document import Document as DocumentModel

    login = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]
    owner_id = uuid_mod.UUID(client.get("/api/auth/me").json()["id"])

    doc_via_library = DocumentModel(uploaded_by=owner_id, title="via-library.txt", checksum="a" * 64, status=IndexStatus.indexed)
    doc_via_documents = DocumentModel(uploaded_by=owner_id, title="via-documents.txt", checksum="b" * 64, status=IndexStatus.indexed)
    superuser_db.add_all([doc_via_library, doc_via_documents])
    superuser_db.commit()

    res_library = client.request(
        "DELETE", f"/api/library/{doc_via_library.id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf}
    )
    res_documents = client.request("DELETE", f"/api/documents/{doc_via_documents.id}", headers={"X-CSRF-Token": csrf})

    assert res_library.status_code == 200
    assert res_documents.status_code == 200
    assert res_library.json() == {"status": "deleted"}
    assert res_documents.json() == {"status": "deleted"}

    superuser_db.expire_all()
    refreshed_library = superuser_db.get(DocumentModel, doc_via_library.id)
    refreshed_documents = superuser_db.get(DocumentModel, doc_via_documents.id)
    # Both routes produced the exact same kind of outcome: soft-deleted, not hard-deleted --
    # proof the older /api/documents route no longer does its old `db.delete(document)`.
    assert refreshed_library is not None and refreshed_library.deleted_at is not None
    assert refreshed_documents is not None and refreshed_documents.deleted_at is not None

    # A repeat delete on either route now 404s cleanly (same SourcePurgeNotFoundError path).
    repeat_library = client.request(
        "DELETE", f"/api/library/{doc_via_library.id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf}
    )
    repeat_documents = client.request("DELETE", f"/api/documents/{doc_via_documents.id}", headers={"X-CSRF-Token": csrf})
    assert repeat_library.status_code == 404
    assert repeat_documents.status_code == 404
