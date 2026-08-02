"""app/rag/source_purge.py::purge_source() — the shared purge service both
app/routers/library.py's delete_source and the older app/routers/documents.py's
delete_document call. Real local Postgres (RLS included), same pattern as
tests/backend/test_memory_source_units.py. HTTP-level "both routes share one service" checks
use the `client`/`superuser_db` fixtures, matching tests/backend/test_library_routes.py.
"""

import importlib.util
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models.document import ActiveTruthStatus, DeletionStatus, Document, DocumentSource, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.memory_source_unit import (
    DocumentSourceUnit,
    LifecycleStatus,
    MemorySourceLifecycleEvent,
    MemorySourceUnit,
    SnapshotStatus,
)
from app.models.project_memory import ProjectCheckpoint, ProjectSource
from app.models.user import User, UserRole
from app.rag.blob_references import (
    DeleteIfUnreferencedOutcome,
    acquire_storage_key_lock,
    delete_if_unreferenced,
    storage_key_still_referenced,
)
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


def _make_import_job(
    session, owner_id, *, source_storage_key, status=ImportJobStatus.pending, blocked_count=0
) -> ImportJob:
    """Pass 22: a durable ImportJob row referencing `source_storage_key` -- knowledge_import_jobs
    is RLS-protected (app/rls.py), same as Document/DocumentChunk, so _set_rls_user is required
    first."""
    _set_rls_user(session, owner_id)
    job = ImportJob(
        owner_id=owner_id,
        status=status,
        source_storage_key=source_storage_key,
        blocked_count=blocked_count,
    )
    session.add(job)
    session.commit()
    return job


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


# --- Pass 22: ImportJob is also a physical blob reference ---------------------------------------
#
# app/rag/blob_references.py::storage_key_still_referenced() -- shared by maybe_purge_blob()
# (app/rag/library_import.py, called from retry_source_blob_purge()) -- now checks
# ImportJob.source_storage_key, not just live Document.storage_key rows. A founder review
# caught the gap: a pending/running/resumable ImportJob's raw upload used to be treated as an
# unreferenced blob the moment no live Document happened to share its content-addressed key.


def test_pending_import_job_blocks_blob_purge_of_shared_storage_key():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"pending import job still needs this")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.pending)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_running_import_job_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"running import job still needs this")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.running)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_blocked_import_job_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"blocked import job still needs this")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.blocked)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_partial_import_job_with_blocked_count_blocks_blob_purge():
    """The 2026-07-28 incident app/worker.py's _requeue_blocked_jobs docstring describes: a ZIP
    job with SOME files genuinely failed and OTHERS paused rolls up to `partial`, not `blocked`
    -- but blocked_count > 0 means the worker's own requeue query still matches it, so the raw
    blob is still needed exactly like a `blocked` job's is."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"partial job with a real blocked file")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(
            session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.partial, blocked_count=1
        )
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_partial_import_job_without_blocked_count_does_not_block_blob_purge():
    """The narrow-not-broad half of the same policy: a `partial` job where nothing is actually
    blocked (blocked_count == 0) must NOT permanently pin the blob -- otherwise every ordinary
    partially-failed ZIP import would leak its raw upload forever."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"partial job, nothing actually blocked")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(
            session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.partial, blocked_count=0
        )
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_terminal_import_job_with_stuck_resumable_sibling_document_blocks_blob_purge():
    """The non-obvious case app/worker.py's _reconcile_orphaned_documents drives: a ZIP job
    already at a terminal status (`completed`) can still be reset back to `pending` if ANY of
    its OWN documents is still stuck mid-pipeline -- a DIFFERENT document than the one being
    purged here, from the very same job. Purging one already-finished sibling must not destroy
    the raw blob the stuck sibling's eventual resumption still needs."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"terminal job, but a sibling document is still stuck")
        document = _make_document(session, owner.id, title="finished-sibling", storage_key=storage_key)
        job = _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.completed)

        stuck_sibling = _make_document(session, owner.id, title="stuck-sibling", storage_key=None)
        _set_rls_user(session, owner.id)
        stuck_sibling.import_job_id = job.id
        stuck_sibling.status = IndexStatus.extracting
        session.add(stuck_sibling)
        session.commit()

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_terminal_import_job_without_stuck_sibling_allows_blob_purge():
    """The mirror case: a terminal job with no stuck document anywhere is correctly treated as
    fully expired -- the blob purges normally, exactly like before ImportJob was ever
    considered a reference at all."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"terminal job, nothing left stuck")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.failed)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cancelled_import_job_does_not_block_blob_purge():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"cancelled job needs nothing")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.cancelled)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_zip_raw_blob_and_document_blob_are_separate_content_addressed_keys():
    """Content-addressing means a ZIP job's raw upload and one of its extracted documents'
    OWN storage_key are separate keys whenever their bytes differ (the ordinary case) --
    purging a document must never mistake a completely unrelated, still-pending job's raw
    blob for its own, and vice versa. Proven directly: a live pending ImportJob references key
    B, unrelated to the document being purged (key A) -- purging A must succeed and leave B's
    blob untouched."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key_a = _store_real_blob(b"this document's own content")
        storage_key_b = _store_real_blob(b"a completely unrelated zip raw upload")
        document = _make_document(session, owner.id, storage_key=storage_key_a)
        _make_import_job(session, owner.id, source_storage_key=storage_key_b, status=ImportJobStatus.pending)
        _set_rls_user(session, owner.id)

        result = purge_source(session, document.id, owner.id)

        assert result.deletion_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key_a)
        assert get_storage().exists(storage_key_b)  # untouched -- a different key entirely
    finally:
        session.rollback()
        session.close()


def test_maybe_purge_blob_delegates_to_the_shared_reference_policy(monkeypatch):
    """Proves app/rag/library_import.py::maybe_purge_blob() does not maintain its own,
    duplicated reference-check logic -- it calls the one canonical
    storage_key_still_referenced() app/rag/blob_references.py owns, which both
    retry_source_blob_purge() (phase B) and POST /api/library/import (the upload path) share."""
    import app.rag.library_import as library_import_module

    calls: list[str] = []
    real_policy = library_import_module.storage_key_still_referenced

    def _tracking_policy(db, storage_key):
        calls.append(storage_key)
        return real_policy(db, storage_key)

    monkeypatch.setattr(library_import_module, "storage_key_still_referenced", _tracking_policy)

    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"delegation proof")
        document = _make_document(session, owner.id, storage_key=storage_key)
        _set_rls_user(session, owner.id)

        purge_source(session, document.id, owner.id)

        assert calls == [storage_key]
    finally:
        session.rollback()
        session.close()


def test_storage_key_lock_serializes_upload_and_purge_so_a_committed_import_job_never_references_a_missing_blob():
    """The TOCTOU race a founder review found: POST /api/library/import's storage.write_stream()
    durably writes a blob to disk BEFORE anything in the database references it -- a concurrent
    retry_source_blob_purge() could run its own reference check + delete in that exact window.
    Reproduced here with two REAL, separate DB sessions/connections and a real Postgres
    advisory lock (acquire_storage_key_lock), not a mocked timer: a "purge" thread acquires the
    lock first and physically deletes the blob while holding it; only after it commits (and so
    releases the lock) does the "upload" thread's own acquire unblock -- at which point it must
    observe the blob is gone and refuse to proceed, exactly like app/routers/library.py's
    import_package does."""
    storage_key = _store_real_blob(b"raced between a concurrent upload and a concurrent purge")
    assert get_storage().exists(storage_key)

    lock_acquired = threading.Event()
    proceed_with_delete = threading.Event()
    purge_committed = threading.Event()

    def _purge_thread():
        session = SessionLocal()
        try:
            acquire_storage_key_lock(session, storage_key)
            lock_acquired.set()
            proceed_with_delete.wait(timeout=5)
            get_storage().delete(storage_key)
            session.commit()  # releases the advisory lock
        finally:
            purge_committed.set()
            session.close()

    purge_thread = threading.Thread(target=_purge_thread)
    purge_thread.start()
    assert lock_acquired.wait(timeout=5), "purge thread never acquired the storage_key lock"

    upload_session = SessionLocal()
    try:
        proceed_with_delete.set()  # let the purge thread proceed to delete + commit
        # Blocks on the SAME advisory lock (a different connection) until the purge thread's
        # transaction above commits -- this is the exact serialization point being tested.
        acquire_storage_key_lock(upload_session, storage_key)
        blob_survived = get_storage().exists(storage_key)
        would_create_import_job = blob_survived
        upload_session.commit()
    finally:
        upload_session.close()

    purge_thread.join(timeout=5)
    assert purge_committed.is_set()
    assert not blob_survived, "the purge thread's delete must have already happened by the time the lock releases"
    assert not would_create_import_job, (
        "the upload path must refuse to create an ImportJob referencing a blob that a concurrent purge already deleted"
    )


# --- Pass 22: the source_purged audit entry moved INTO phase A's own atomic transaction --------


def test_forced_audit_failure_rolls_back_the_entire_phase_a_purge(monkeypatch):
    """The audit row is no longer written by a separate, later commit in the router -- it's
    added to the session as PART of phase A, right before phase A's own single commit (see
    source_purge.py's purge_source()). A failure recording it must therefore roll back
    everything else phase A did too, exactly like any other phase A failure (Pass 21's
    test_phase_a_db_commit_failure_leaves_blob_untouched_and_never_calls_storage_delete proved
    the general case; this proves the audit write specifically is inside that same
    all-or-nothing boundary, not bolted on after it)."""
    import app.rag.source_purge as source_purge_module
    from app.storage.local_fs import LocalFilesystemStorage

    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"audit failure must not leave a half-done purge")
        document = _make_document(session, owner.id, storage_key=storage_key)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        _set_rls_user(session, owner.id)

        delete_calls: list[str] = []
        real_delete = LocalFilesystemStorage.delete

        def _tracking_delete(self, key):
            delete_calls.append(key)
            return real_delete(self, key)

        def _boom_record_audit(*args, **kwargs):
            raise RuntimeError("simulated audit insert failure")

        LocalFilesystemStorage.delete = _tracking_delete
        monkeypatch.setattr(source_purge_module, "record_audit", _boom_record_audit)
        try:
            with pytest.raises(RuntimeError, match="simulated audit insert failure"):
                purge_source(session, document.id, owner.id)
        finally:
            LocalFilesystemStorage.delete = real_delete

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


def test_successful_purge_writes_exactly_one_audit_entry_from_either_http_route(client, superuser_db):
    """Both app/routers/library.py's delete_source and app/routers/documents.py's
    delete_document now rely on purge_source() itself to write the source_purged audit row
    (Pass 22) -- neither router calls record_audit() a second time on top of it. Verified at
    the HTTP level, against the real audit_log table, for both routes."""
    import uuid as uuid_mod

    from app.models.audit import AuditLog
    from app.models.document import Document as DocumentModel

    login = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"})
    assert login.status_code == 200
    csrf = login.json()["csrf_token"]
    owner_id = uuid_mod.UUID(client.get("/api/auth/me").json()["id"])

    doc_via_library = DocumentModel(uploaded_by=owner_id, title="audit-library.txt", checksum="c" * 64, status=IndexStatus.indexed)
    doc_via_documents = DocumentModel(
        uploaded_by=owner_id, title="audit-documents.txt", checksum="d" * 64, status=IndexStatus.indexed
    )
    superuser_db.add_all([doc_via_library, doc_via_documents])
    superuser_db.commit()

    res_library = client.request(
        "DELETE", f"/api/library/{doc_via_library.id}", json={"confirm": True}, headers={"X-CSRF-Token": csrf}
    )
    res_documents = client.request("DELETE", f"/api/documents/{doc_via_documents.id}", headers={"X-CSRF-Token": csrf})
    assert res_library.status_code == 200
    assert res_documents.status_code == 200

    library_audit_rows = (
        superuser_db.query(AuditLog)
        .filter_by(action="source_purged", entity_type="document", entity_id=str(doc_via_library.id))
        .all()
    )
    documents_audit_rows = (
        superuser_db.query(AuditLog)
        .filter_by(action="source_purged", entity_type="document", entity_id=str(doc_via_documents.id))
        .all()
    )
    assert len(library_audit_rows) == 1
    assert len(documents_audit_rows) == 1
    assert library_audit_rows[0].user_id == owner_id
    assert documents_audit_rows[0].user_id == owner_id


# --- Pass 23: cross-owner blob-reference visibility (storage_key_still_referenced_global) ------
#
# Content-addressed blob storage is GLOBAL -- two different owners' byte-identical uploads
# share the exact same storage_key. documents/knowledge_import_jobs both have FORCE ROW LEVEL
# SECURITY with owner-scoped policies, so an ordinary ORM query run inside owner A's RLS
# session structurally cannot see owner B's rows. Every test below runs through the REAL
# mainai_app-bound SessionLocal (RLS included, exactly like every other test in this file) --
# proving the fix actually crosses the RLS boundary via storage_key_still_referenced_global(),
# not by disabling RLS for the test itself.


def test_cross_owner_live_document_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a1@example.com")
        owner_b = _make_user(session, email="purge-cross-b1@example.com")
        storage_key = _store_real_blob(b"shared across two different owners -- live document")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        _make_document(session, owner_b.id, title="owner-b-live", storage_key=storage_key)
        _set_rls_user(session, owner_a.id)

        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_pending_import_job_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a2@example.com")
        owner_b = _make_user(session, email="purge-cross-b2@example.com")
        storage_key = _store_real_blob(b"owner b has a pending import job for this")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        _make_import_job(session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.pending)
        _set_rls_user(session, owner_a.id)

        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_running_import_job_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a3@example.com")
        owner_b = _make_user(session, email="purge-cross-b3@example.com")
        storage_key = _store_real_blob(b"owner b has a running import job for this")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        _make_import_job(session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.running)
        _set_rls_user(session, owner_a.id)

        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_blocked_import_job_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a4@example.com")
        owner_b = _make_user(session, email="purge-cross-b4@example.com")
        storage_key = _store_real_blob(b"owner b has a blocked import job for this")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        _make_import_job(session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.blocked)
        _set_rls_user(session, owner_a.id)

        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_partial_with_blocked_count_blocks_blob_purge():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a5@example.com")
        owner_b = _make_user(session, email="purge-cross-b5@example.com")
        storage_key = _store_real_blob(b"owner b has a partial job with a real blocked file")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        _make_import_job(
            session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.partial, blocked_count=1
        )
        _set_rls_user(session, owner_a.id)

        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_terminal_job_with_resumable_sibling_blocks_blob_purge():
    """The non-obvious case, now proven across owners too: owner B's job is terminal
    (`completed`) but one of B's OWN documents is still stuck mid-pipeline -- app/worker.py's
    _reconcile_orphaned_documents would still resume it. Owner A purging their own,
    unrelated document sharing the same content-addressed key must not destroy the blob B's
    stuck document still needs."""
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a6@example.com")
        owner_b = _make_user(session, email="purge-cross-b6@example.com")
        storage_key = _store_real_blob(b"owner b terminal job, but a sibling document is stuck")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        job_b = _make_import_job(session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.completed)

        stuck_sibling_b = _make_document(session, owner_b.id, title="owner-b-stuck-sibling", storage_key=None)
        _set_rls_user(session, owner_b.id)
        stuck_sibling_b.import_job_id = job_b.id
        stuck_sibling_b.status = IndexStatus.extracting
        session.add(stuck_sibling_b)
        session.commit()

        _set_rls_user(session, owner_a.id)
        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_terminal_job_without_resumable_sibling_does_not_block():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a7@example.com")
        owner_b = _make_user(session, email="purge-cross-b7@example.com")
        storage_key = _store_real_blob(b"owner b terminal job, nothing left stuck")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        _make_import_job(session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.failed)
        _set_rls_user(session, owner_a.id)

        result = purge_source(session, document_a.id, owner_a.id)

        assert result.deletion_status == DeletionStatus.purged
        assert not get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_cross_owner_last_global_reference_removed_allows_purge():
    """The full lifecycle across two owners: A's purge is blocked while B's job is still
    pending; once B's job itself is no longer a live reference (advanced to a terminal,
    non-resumable state), A's retry can finally purge the shared blob."""
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a8@example.com")
        owner_b = _make_user(session, email="purge-cross-b8@example.com")
        storage_key = _store_real_blob(b"blocked until owner b's last reference disappears")
        document_a = _make_document(session, owner_a.id, storage_key=storage_key)
        job_b = _make_import_job(session, owner_b.id, source_storage_key=storage_key, status=ImportJobStatus.pending)
        _set_rls_user(session, owner_a.id)

        first = purge_source(session, document_a.id, owner_a.id)
        assert first.deletion_status == DeletionStatus.pending
        assert get_storage().exists(storage_key)

        _set_rls_user(session, owner_b.id)
        job_b.status = ImportJobStatus.failed
        session.add(job_b)
        session.commit()

        _set_rls_user(session, owner_a.id)
        retried = retry_source_blob_purge(session, document_a.id, owner_a.id)

        assert retried == DeletionStatus.purged
        assert not get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_mainai_app_gets_only_a_boolean_and_cannot_read_other_owners_rows_directly():
    """The information-minimality guarantee: mainai_app CAN call
    storage_key_still_referenced_global() and get a correct cross-owner answer (proven by all
    the tests above), but an ordinary query in the SAME owner-scoped session still cannot see
    a different owner's Document/ImportJob rows directly -- RLS itself is untouched, only this
    one narrow function bypasses it internally."""
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="purge-cross-a9@example.com")
        owner_b = _make_user(session, email="purge-cross-b9@example.com")
        storage_key = _store_real_blob(b"boolean-only proof")
        _make_document(session, owner_b.id, title="owner-b-invisible", storage_key=storage_key)
        _set_rls_user(session, owner_a.id)

        still_referenced = storage_key_still_referenced(session, storage_key)
        assert still_referenced is True  # the function itself sees across owners

        # But an ordinary ORM query in owner A's own RLS session cannot see owner B's row.
        visible_to_a = session.query(Document).filter(Document.storage_key == storage_key).all()
        assert visible_to_a == []
    finally:
        session.rollback()
        session.close()


# --- Pass 29: cross-domain blob-reference visibility (Project Memory) --------------------------
#
# Content-addressed storage is global not just across OWNERS (Pass 23 above) but across
# completely unrelated DATA DOMAINS: app/project_memory.py's founder-wide ProjectSource/
# ProjectCheckpoint rows write through the exact same get_storage()/write_stream() backend as
# Document/ImportJob. A founder review found storage_key_still_referenced_global() (migration
# 0020) never checked either table -- migration 0023 adds both. These tests prove the SQL
# function's behavior directly (not through purge_source(), which only ever touches Document/
# ImportJob rows) -- see test_account_erasure.py's Pass 29 section for the end-to-end proof
# through a REAL account erasure (test C/D here, test A/B there, per the founder's own
# lettering).


def test_storage_key_still_referenced_global_sees_a_project_source_after_the_last_document_is_gone():
    """Test C (founder's lettering): once every Document/ImportJob reference to a key is gone,
    a ProjectSource still pointing at the SAME (shared, content-addressed) key must keep the
    function returning true -- the exact case migration 0020 was blind to."""
    session = SessionLocal()
    try:
        storage_key = _store_real_blob(b"pass 29: project-memory-only reference after documents are gone")
        session.add(
            ProjectSource(source_type="doc", source_ref="docs/EXAMPLE.md", storage_key=storage_key, ingested_by="test")
        )
        session.commit()

        still_referenced = storage_key_still_referenced(session, storage_key)

        assert still_referenced is True
    finally:
        session.rollback()
        session.close()


def test_storage_key_still_referenced_global_sees_a_project_checkpoint():
    """Same proof for ProjectCheckpoint.brief_storage_key (the resumption-brief blob) --
    distinct column, distinct table, same shared storage backend."""
    session = SessionLocal()
    try:
        storage_key = _store_real_blob(b"pass 29: project checkpoint brief reference")
        session.add(
            ProjectCheckpoint(
                summary="test checkpoint",
                branch_name="main",
                open_pr_refs="",
                brief_storage_key=storage_key,
                brief_sha256="a" * 64,
                created_by="test",
            )
        )
        session.commit()

        still_referenced = storage_key_still_referenced(session, storage_key)

        assert still_referenced is True
    finally:
        session.rollback()
        session.close()


def test_storage_key_still_referenced_global_returns_false_once_every_domain_has_let_go():
    """Test D (founder's lettering): a key referenced by NEITHER Document/ImportJob NOR
    ProjectSource/ProjectCheckpoint must correctly return false -- the function isn't just
    permanently true once any domain ever touched it; each domain's reference is checked
    live, and the key becomes purgeable the moment none of them still hold it."""
    session = SessionLocal()
    try:
        storage_key = _store_real_blob(b"pass 29: no domain references this at all")

        still_referenced = storage_key_still_referenced(session, storage_key)

        assert still_referenced is False
    finally:
        session.rollback()
        session.close()


def test_known_storage_key_columns_registry_matches_the_sql_functions_real_behavior():
    """Pass 29 drift-prevention: iterates `app.rag.blob_references.KNOWN_STORAGE_KEY_COLUMNS`
    -- the canonical, hand-maintained list of every table.column that can hold a live
    reference into the shared content-addressed storage backend -- and proves
    `storage_key_still_referenced_global()` actually protects EVERY one of them, not just the
    ones a human remembers to test by hand. A future storage_key-shaped column added to the
    registry without a matching SQL-function update fails this test immediately, rather than
    silently reopening the exact cross-domain orphan-blob gap Pass 29 closed."""
    from app.rag.blob_references import KNOWN_STORAGE_KEY_COLUMNS

    session = SessionLocal()
    try:
        owner = _make_user(session, email=f"drift-registry-{uuid.uuid4().hex[:8]}@example.com")

        def _make_live_row(table: str, column: str, storage_key: str) -> None:
            if table == "documents" and column == "storage_key":
                _make_document(session, owner.id, storage_key=storage_key)
            elif table == "knowledge_import_jobs" and column == "source_storage_key":
                _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.pending)
            elif table == "project_sources" and column == "storage_key":
                session.add(
                    ProjectSource(source_type="doc", source_ref="drift-test", storage_key=storage_key, ingested_by="test")
                )
                session.commit()
            elif table == "project_checkpoints" and column == "brief_storage_key":
                session.add(
                    ProjectCheckpoint(
                        summary="drift test",
                        branch_name="main",
                        open_pr_refs="",
                        brief_storage_key=storage_key,
                        brief_sha256="a" * 64,
                        created_by="test",
                    )
                )
                session.commit()
            else:
                pytest.fail(
                    f"KNOWN_STORAGE_KEY_COLUMNS has an entry ({table}.{column}) this test doesn't know how to "
                    "construct a live row for -- add a branch above (see migration 0023's own module docstring's "
                    "'adding a new column' checklist)."
                )

        for table, column in KNOWN_STORAGE_KEY_COLUMNS:
            storage_key = _store_real_blob(f"drift registry proof: {table}.{column}".encode())
            _make_live_row(table, column, storage_key)

            still_referenced = storage_key_still_referenced(session, storage_key)

            assert still_referenced is True, (
                f"{table}.{column} is registered in KNOWN_STORAGE_KEY_COLUMNS but "
                "storage_key_still_referenced_global() did not report it as referenced -- "
                "the SQL function is missing coverage for this column"
            )
    finally:
        session.rollback()
        session.close()


# --- Pass 30 (a fourth founder review round): delete_if_unreferenced() -- the canonical,
# self-contained check-then-act delete for a blob with no durable DB row yet (see app/rag/
# blob_references.py's own docstring for the empty-upload incident this closes). Tests E/F
# below are the founder's own lettering (A-D live in tests/backend/test_library_routes.py's
# Pass 30 section, exercised through the real HTTP endpoint).


def test_delete_if_unreferenced_race_against_a_concurrent_reference_commit_never_leaves_a_dangling_reference(superuser_db):
    """Test E (founder's lettering): a REAL two-thread, two-session race between
    delete_if_unreferenced() and a reference-creating commit for the SAME key, both
    disciplined participants in the SAME acquire_storage_key_lock() protocol. Whichever side
    wins the real Postgres advisory lock race must fully commit or roll back before the
    other's own check-then-act can run -- the end state must NEVER be "a live DB row
    references a storage_key whose physical blob is gone."

    A note on what this test does NOT cover, found while building it (not asked for this
    round, documented in docs/BRANCH_REGISTRY.md's Pass 30 entry as a distinct, separate
    finding rather than silently expanded into this fix): app/routers/library.py's REAL
    upload sequence calls `storage.write_stream()` BEFORE acquiring
    `acquire_storage_key_lock()` (the key isn't even known until the bytes are hashed, so it
    structurally can't be locked any earlier). A first draft of this test reproduced that
    exact real ordering (write_stream, THEN lock, THEN commit) and found a real, narrower
    TOCTOU purely inside `LocalFilesystemStorage.write_stream()`'s own existence-check vs. a
    concurrent `delete()`'s `unlink()` -- both file-level operations that happen OUTSIDE any
    lock (the lock only ever protects the DB-facing commit/delete decision, never the raw
    filesystem calls themselves). Closing that would mean restructuring write_stream() itself
    (e.g. hashing to a value BEFORE deciding whether to write, then holding the storage-key
    lock across the existence-check-and-rename) -- a real architectural change to the
    SAME-SHAPED protocol `app/rag/source_purge.py`'s already-shipped, previously-reviewed
    Phase B relies on too, not something specific to this pass's `delete_if_unreferenced()`.
    Out of scope for "close the empty-upload direct-delete bypass"; flagged for its own
    review. The test below instead proves the narrower, well-defined guarantee THIS pass
    actually owns: once both sides are inside the lock-protected commit/delete decision (the
    part `delete_if_unreferenced()` and library.py's real ImportJob/Document commit both
    genuinely share), the advisory lock correctly serializes them with no dangling result."""
    from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus

    def _run_once(*, cleanup_goes_first: bool) -> None:
        owner = _make_user(SessionLocal(), email=f"race-e-{uuid.uuid4().hex[:8]}@example.com")
        content = f"pass 30 test E race proof {uuid.uuid4().hex}".encode()
        storage_key = _store_real_blob(content)

        lock_acquired = threading.Event()
        proceed = threading.Event()
        winner_done = threading.Event()

        def _winner_thread():
            db = SessionLocal()
            try:
                acquire_storage_key_lock(db, storage_key)
                lock_acquired.set()
                proceed.wait(timeout=5)
                if cleanup_goes_first:
                    delete_if_unreferenced(db, get_storage(), storage_key)
                    db.commit()
                else:
                    # Bytes are re-verified/re-materialized WHILE still holding the lock, so
                    # no concurrent delete can interleave between the write and the commit --
                    # see the docstring above for why this is stricter than production's real
                    # (unlocked) write_stream() ordering, deliberately, to isolate exactly
                    # what delete_if_unreferenced() itself is responsible for.
                    get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content))
                    _set_rls_user(db, owner.id)
                    document = Document(
                        title="race winner",
                        source=DocumentSource.upload,
                        uploaded_by=owner.id,
                        active_truth_status=ActiveTruthStatus.active,
                        status=IndexStatus.indexed,
                        storage_key=storage_key,
                    )
                    db.add(document)
                    db.commit()
            finally:
                winner_done.set()
                db.close()

        t = threading.Thread(target=_winner_thread)
        t.start()
        assert lock_acquired.wait(timeout=5), "winner thread never acquired the storage-key lock"

        loser_session = SessionLocal()
        try:
            proceed.set()
            # Blocks on the SAME real Postgres advisory lock (a different connection) until
            # the winner thread's transaction above commits -- the exact serialization this
            # test proves.
            acquire_storage_key_lock(loser_session, storage_key)
            if cleanup_goes_first:
                get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content))
                _set_rls_user(loser_session, owner.id)
                document = Document(
                    title="race loser",
                    source=DocumentSource.upload,
                    uploaded_by=owner.id,
                    active_truth_status=ActiveTruthStatus.active,
                    status=IndexStatus.indexed,
                    storage_key=storage_key,
                )
                loser_session.add(document)
                loser_session.commit()
            else:
                delete_if_unreferenced(loser_session, get_storage(), storage_key)
                loser_session.commit()
        finally:
            t.join(timeout=5)

        assert winner_done.is_set(), "winner thread never completed -- possible deadlock"

        # The invariant that actually matters, regardless of which side won: a live Document
        # row referencing storage_key must NEVER coexist with a missing physical blob.
        superuser_db.expire_all()
        any_document_references_key = superuser_db.query(Document).filter_by(storage_key=storage_key).count() > 0
        blob_exists = get_storage().exists(storage_key)
        assert not (any_document_references_key and not blob_exists), (
            "a Document references a storage_key whose physical blob is gone -- exactly the "
            "dangling-reference outcome the lock protocol must prevent"
        )

    _run_once(cleanup_goes_first=True)
    _run_once(cleanup_goes_first=False)


def test_delete_if_unreferenced_logs_and_returns_failed_on_a_genuine_storage_error_without_crashing(monkeypatch):
    """Test F (founder's lettering): a StorageError deleting an ALREADY-CONFIRMED-unreferenced
    blob must never look like a false success, never leave an unhandled exception propagating
    out of the request, and must never leave the advisory lock held past this function's own
    return (it's transaction-scoped -- released at the caller's own next commit/rollback,
    which app/routers/library.py's empty-upload branch performs unconditionally regardless of
    outcome, proven separately by the HTTP-level tests in test_library_routes.py)."""
    storage_key = _store_real_blob(b"pass 30 test F storage error proof")

    class _AlwaysBrokenStorage:
        def delete(self, key):
            raise StorageError("simulated permanent failure deleting an already-unreferenced blob")

    session = SessionLocal()
    try:
        outcome = delete_if_unreferenced(session, _AlwaysBrokenStorage(), storage_key)  # must not raise

        assert outcome == DeleteIfUnreferencedOutcome.failed
        session.commit()  # releases the advisory lock -- must not itself raise
    finally:
        session.rollback()
        session.close()


def test_every_direct_storage_delete_call_site_is_on_the_known_allowlist():
    """Pass 30 drift-prevention (founder's explicit request, point 4): storage_key_still_
    referenced_global()/delete_if_unreferenced() can only ever protect a physical delete that
    actually GOES THROUGH the canonical check-then-act protocol -- neither can protect a
    `storage.delete(...)` call that bypasses them entirely, exactly like app/routers/
    library.py's empty-upload path used to (see app/rag/blob_references.py's
    delete_if_unreferenced() docstring for that incident). This walks the AST of every .py
    file under app/ and finds every call of the shape `storage.delete(...)` -- the exact
    naming convention every real call site in this codebase already uses for a
    `StorageBackend` instance (`storage = get_storage()` then `storage.delete(...)`) -- and
    asserts the exact set of (file, enclosing function) locations matches a hand-maintained
    allowlist below. A NEW direct call site (or one hidden behind a differently-named local
    variable, which this scan cannot see -- a known, accepted limitation, not a silent gap:
    the convention itself is what's being enforced) fails this test immediately, forcing an
    explicit reviewed decision instead of silently reopening this exact bug class."""
    import ast

    app_root = Path(__file__).resolve().parent.parent.parent / "app"

    # (relative file path, enclosing function name): why it's safe to call storage.delete()
    # directly here, not through delete_if_unreferenced()/maybe_purge_blob().
    ALLOWED_CALL_SITES = {
        # Caller holds acquire_storage_key_lock() for the whole check-then-act sequence
        # (documented in its own docstring); gated by storage_key_still_referenced() just
        # above the delete() call.
        ("rag/library_import.py", "maybe_purge_blob"),
        # Same pattern, inside app/rag/account_erasure.py's own claim/lease-protected task
        # processing -- gated by storage_key_still_referenced() just above.
        ("rag/account_erasure.py", "attempt_storage_deletion_task"),
        # The canonical, self-contained helper itself (Pass 30) -- acquires the lock and
        # performs the reference check internally; this IS the sanctioned call site every
        # other caller with no existing DB row should route through.
        ("rag/blob_references.py", "delete_if_unreferenced"),
    }

    class _FunctionTrackingVisitor(ast.NodeVisitor):
        def __init__(self):
            self.found: set[tuple[str, str]] = set()
            self._stack: list[str] = []

        def _visit_function(self, node):
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Call(self, node):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "storage"
            ):
                enclosing = self._stack[-1] if self._stack else "<module>"
                self.found.add((self._current_file, enclosing))
            self.generic_visit(node)

    found_sites: set[tuple[str, str]] = set()
    for path in app_root.rglob("*.py"):
        source = path.read_text()
        if "storage.delete(" not in source:
            continue
        visitor = _FunctionTrackingVisitor()
        visitor._current_file = str(path.relative_to(app_root))
        visitor.visit(ast.parse(source, filename=str(path)))
        found_sites |= visitor.found

    assert found_sites == ALLOWED_CALL_SITES, (
        f"storage.delete() call sites drifted from the reviewed allowlist.\n"
        f"New/unexpected: {found_sites - ALLOWED_CALL_SITES}\n"
        f"Missing (removed or renamed): {ALLOWED_CALL_SITES - found_sites}"
    )


def test_public_lacks_execute_on_storage_key_still_referenced_global():
    session = SessionLocal()
    try:
        has_execute = session.execute(
            sa_text(
                "SELECT has_function_privilege('public', 'public.storage_key_still_referenced_global(text)', 'EXECUTE')"
            )
        ).scalar()
        assert has_execute is False
    finally:
        session.rollback()
        session.close()


def test_apply_runtime_privileges_verifies_storage_key_function_owner_has_bypassrls():
    """storage_key_still_referenced_global() has no per-caller ownership check by design (see
    its module docstring) -- it relies entirely on its owning role genuinely having
    BYPASSRLS (or being superuser), exactly like transition_memory_source_admin/
    erase_owner_memory_admin already do. Proven here by actually reassigning ownership to a
    fresh, deliberately non-superuser/non-BYPASSRLS role and confirming apply_and_verify()
    catches it -- not assumed. Same pattern as
    tests/backend/test_memory_source_units.py::test_apply_runtime_privileges_verifies_admin_function_owner_has_bypassrls."""
    from sqlalchemy import text as _sa_text

    settings = get_settings()
    engine = create_engine(settings.database_url)
    weak_role = "s1a_test_weak_owner_no_bypassrls_p23"
    try:
        with engine.begin() as conn:
            conn.execute(_sa_text(f"DROP ROLE IF EXISTS {weak_role}"))
            conn.execute(_sa_text(f"CREATE ROLE {weak_role} NOSUPERUSER NOBYPASSRLS"))
            conn.execute(
                _sa_text(f"ALTER FUNCTION public.storage_key_still_referenced_global(text) OWNER TO {weak_role}")
            )

        module = _load_apply_runtime_privileges()
        with pytest.raises(SystemExit):
            module.apply_and_verify(settings.database_url)
    finally:
        admin_role = engine.url.username
        with engine.begin() as conn:
            conn.execute(
                _sa_text(f"ALTER FUNCTION public.storage_key_still_referenced_global(text) OWNER TO {admin_role}")
            )
            conn.execute(_sa_text(f"DROP ROLE IF EXISTS {weak_role}"))
        module = _load_apply_runtime_privileges()
        module.apply_and_verify(settings.database_url)
        engine.dispose()


def test_apply_runtime_privileges_catches_security_invoker_downgrade():
    """Pass 24: a founder review pointed out that nothing previously verified
    `storage_key_still_referenced_global()` was still actually SECURITY DEFINER at all --
    owner, BYPASSRLS, search_path and grants could all stay exactly correct while `ALTER
    FUNCTION ... SECURITY INVOKER` silently made the function run with the CALLER's
    (mainai_app's) privileges again, re-subjecting its own queries to mainai_app's ordinary
    RLS scope and reintroducing the exact cross-owner gap Pass 23 closed -- with boot
    verification still reporting success throughout. Proven here by actually flipping the
    function to SECURITY INVOKER and confirming apply_and_verify() catches it."""
    from sqlalchemy import text as _sa_text

    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(_sa_text("ALTER FUNCTION public.storage_key_still_referenced_global(text) SECURITY INVOKER"))

        module = _load_apply_runtime_privileges()
        with pytest.raises(SystemExit):
            module.apply_and_verify(settings.database_url)
    finally:
        with engine.begin() as conn:
            conn.execute(_sa_text("ALTER FUNCTION public.storage_key_still_referenced_global(text) SECURITY DEFINER"))
        module = _load_apply_runtime_privileges()
        module.apply_and_verify(settings.database_url)
        engine.dispose()


def test_apply_runtime_privileges_verifies_return_type_and_language():
    """Directly proves the new return-type/language catalog checks fire, not just that they
    exist in the code -- if `storage_key_still_referenced_global()`'s return type were ever
    anything other than `boolean` (e.g. a future refactor accidentally widened it to return a
    row, a text summary, or anything else), the whole information-minimality guarantee (see
    migration 0020's module docstring: "returns nothing but a boolean") would be silently
    broken. `s1a_privilege_policy.apply_privilege_policy()` is called directly here (not
    through a real ALTER, since Postgres has no cheap way to change just the return type of
    an existing function without a DROP+CREATE) with a monkeypatched `_FUNCTIONS` expecting
    the WRONG return type, and must report a real error."""
    import importlib.util
    from pathlib import Path as _Path

    import psycopg2

    from app.config import get_settings as _get_settings

    policy_path = _Path(__file__).resolve().parent.parent.parent / "scripts" / "s1a_privilege_policy.py"
    spec = importlib.util.spec_from_file_location("s1a_privilege_policy_wrongtype", policy_path)
    policy_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy_module)

    policy_module._FUNCTIONS = [
        ("storage_key_still_referenced_global", True, True, "text", ("text",)),  # wrong -- real type is boolean
    ]

    settings = _get_settings()
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = policy_module.apply_privilege_policy(cur, expected_owner=expected_owner, require_complete=False)
        conn.rollback()
    finally:
        conn.close()

    assert any("return type" in e and "boolean" in e for e in errors)


# --- Pass 25: exact function signature verification ----------------------------------------
#
# A founder review caught a real gap in Pass 24's SECURITY DEFINER work: `_FUNCTIONS` carried
# a function name and expected return type, but never the expected ARGUMENT types --
# `_function_signature()` resolved by name alone and simply accepted whatever single overload
# happened to exist. A same-named function with the WRONG arguments (e.g.
# `storage_key_still_referenced_global(integer)` instead of the application's real `(text)`
# call) could pass every other check -- SECURITY DEFINER, boolean return, correct owner,
# correct grants -- while being an entirely different function than the one
# blob_references.py actually calls, which would only break at runtime.
#
# These tests exercise `s1a_privilege_policy._resolve_function()`'s exact-signature logic in
# isolation, against a throwaway scratch function (never a real S1A object), all inside a
# single psycopg2 transaction that is ALWAYS rolled back -- CREATE FUNCTION is itself
# transactional DDL, so the scratch function (and any GRANT/REVOKE the policy issues against
# it) never persists, with no explicit DROP needed.


def _load_policy_module(name: str):
    policy_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "s1a_privilege_policy.py"
    spec = importlib.util.spec_from_file_location(name, policy_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_scratch_policy(cur, functions):
    import psycopg2

    policy_module = _load_policy_module(f"s1a_privilege_policy_sigcheck_{uuid.uuid4().hex}")
    policy_module._FUNCTIONS = functions
    cur.execute("SELECT current_user")
    (expected_owner,) = cur.fetchone()
    return policy_module.apply_privilege_policy(cur, expected_owner=expected_owner, require_complete=False), psycopg2


_SCRATCH_FN = "s1a_test_sig_check_p25"


def test_resolve_function_accepts_the_correct_signature():
    """A: the correct function, with the correct argument types, resolves cleanly and is
    granted/verified exactly like any real managed function -- no signature-related error."""
    import psycopg2

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE FUNCTION public.{_SCRATCH_FN}(text) RETURNS boolean
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
                AS $$ BEGIN RETURN true; END; $$
                """
            )
            errors, _ = _apply_scratch_policy(
                cur, [(_SCRATCH_FN, True, True, "boolean", ("text",))]
            )
        conn.rollback()
    finally:
        conn.close()

    assert errors == []


def test_resolve_function_rejects_an_extra_overload_of_the_same_name():
    """B: TWO overloads of the same function name exist (the expected `(text)` one, plus an
    unexpected `(integer)` one) -- refused entirely, not just "the right one happens to also
    be there so it's fine". A pre-existing PUBLIC grant on the unexpected overload is used to
    prove nothing was touched: if the policy had silently picked one overload to act on, this
    grant would have been revoked."""
    import psycopg2

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE FUNCTION public.{_SCRATCH_FN}(text) RETURNS boolean
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
                AS $$ BEGIN RETURN true; END; $$
                """
            )
            cur.execute(
                f"""
                CREATE FUNCTION public.{_SCRATCH_FN}(integer) RETURNS boolean
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
                AS $$ BEGIN RETURN true; END; $$
                """
            )
            cur.execute(f"GRANT EXECUTE ON FUNCTION public.{_SCRATCH_FN}(integer) TO PUBLIC")

            errors, _ = _apply_scratch_policy(
                cur, [(_SCRATCH_FN, True, True, "boolean", ("text",))]
            )

            cur.execute(
                f"SELECT has_function_privilege('public', 'public.{_SCRATCH_FN}(integer)', 'EXECUTE')"
            )
            (integer_overload_still_public,) = cur.fetchone()
        conn.rollback()
    finally:
        conn.close()

    assert any("expected exactly one overload" in e for e in errors)
    assert integer_overload_still_public is True  # untouched -- the policy refused to act at all


def test_resolve_function_rejects_the_only_overload_when_its_signature_is_wrong():
    """C: only a WRONG-signature overload exists (`(integer)`, application expects `(text)`) --
    must be treated as missing/wrong, never silently accepted as "the one overload that
    happens to be there"."""
    import psycopg2

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE FUNCTION public.{_SCRATCH_FN}(integer) RETURNS boolean
                LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
                AS $$ BEGIN RETURN true; END; $$
                """
            )
            errors, _ = _apply_scratch_policy(
                cur, [(_SCRATCH_FN, True, True, "boolean", ("text",))]
            )
        conn.rollback()
    finally:
        conn.close()

    assert any(
        "the only overload present" in e and "expects" in e and "(text)" in e for e in errors
    )


def test_resolve_function_finds_the_right_signature_but_still_catches_security_invoker():
    """D: the correct name AND the correct argument types resolve successfully (proving exact-
    signature resolution doesn't itself get confused by SECURITY INVOKER), but the function is
    SECURITY INVOKER -- still caught by the downstream prosecdef check, exactly like the real
    storage_key_still_referenced_global() INVOKER-downgrade test above, just against an
    isolated scratch function instead of touching the real one."""
    import psycopg2

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE FUNCTION public.{_SCRATCH_FN}(text) RETURNS boolean
                LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog
                AS $$ BEGIN RETURN true; END; $$
                """
            )
            errors, _ = _apply_scratch_policy(
                cur, [(_SCRATCH_FN, True, True, "boolean", ("text",))]
            )
        conn.rollback()
    finally:
        conn.close()

    assert any("SECURITY DEFINER" in e and "prosecdef=false" in e for e in errors)


# --- Pass 24: status-drift protection ------------------------------------------------------
#
# storage_key_still_referenced_global() (migration 0020) hardcodes the same string literals
# as app.models.document.RESUMABLE_INDEX_STATUSES and the same ImportJobStatus rules
# blob_references.py's module docstring documents -- there is structurally no way for a SQL
# function to import a Python frozenset, so the two lists can only be kept in sync by hand.
# These tests exhaustively compare the SQL function's real, observed behavior against the
# Python contract for EVERY enum value that exists today, so that adding a new IndexStatus or
# ImportJobStatus value without updating migration 0020 in the same change fails a test
# immediately, rather than silently reopening a cross-owner/blob-deletion gap later.


def test_sibling_document_status_policy_matches_resumable_index_statuses_for_every_status():
    from app.models.document import RESUMABLE_INDEX_STATUSES

    session = SessionLocal()
    try:
        for status in IndexStatus:
            owner = _make_user(session, email=f"purge-drift-{status.value}@example.com")
            storage_key = _store_real_blob(f"status drift check: {status.value}".encode())
            document = _make_document(session, owner.id, storage_key=storage_key)
            job = _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.completed)

            sibling = _make_document(session, owner.id, title=f"sibling-{status.value}", storage_key=None)
            _set_rls_user(session, owner.id)
            sibling.import_job_id = job.id
            sibling.status = status
            session.add(sibling)
            session.commit()

            _set_rls_user(session, owner.id)
            result = purge_source(session, document.id, owner.id)

            expected_blocked = status in RESUMABLE_INDEX_STATUSES
            actually_blocked = result.deletion_status == DeletionStatus.pending
            assert actually_blocked == expected_blocked, (
                f"IndexStatus.{status.value}: expected blocked={expected_blocked} "
                f"(RESUMABLE_INDEX_STATUSES membership) but the SQL policy gave "
                f"deletion_status={result.deletion_status.value} -- migration 0020's hardcoded "
                f"status list has drifted from app.models.document.RESUMABLE_INDEX_STATUSES"
            )
            assert get_storage().exists(storage_key) == expected_blocked
    finally:
        session.rollback()
        session.close()


def test_import_job_status_policy_matches_the_documented_contract_for_every_status():
    """The job-status half of the same contract, independent of any sibling document.

    Pass 25: the expectation used to be a second, hand-typed `pending/running/blocked`
    if/elif chain living only in this test -- structurally uncoupled from the actual policy,
    so a future status added to the worker's real resumption paths without a matching update
    HERE could still pass. Now imports app.models.import_job's own
    import_job_still_needs_raw_blob() -- the same canonical predicate app/jobs/lease.py's
    claim_next_job and app/worker.py's _requeue_blocked_jobs build their real SQL from -- and
    compares the SQL policy's observed behavior directly against IT, so this test is coupled
    to the real policy's single source of truth, not a second copy of it."""
    from app.models.import_job import import_job_still_needs_raw_blob

    session = SessionLocal()
    try:
        for status in ImportJobStatus:
            for blocked_count in (0, 1):
                owner = _make_user(session, email=f"purge-jobdrift-{status.value}-{blocked_count}@example.com")
                storage_key = _store_real_blob(f"job status drift: {status.value}-{blocked_count}".encode())
                document = _make_document(session, owner.id, storage_key=storage_key)
                _make_import_job(
                    session, owner.id, source_storage_key=storage_key, status=status, blocked_count=blocked_count
                )
                _set_rls_user(session, owner.id)

                result = purge_source(session, document.id, owner.id)

                expected_blocked = import_job_still_needs_raw_blob(status, blocked_count)
                actually_blocked = result.deletion_status == DeletionStatus.pending
                assert actually_blocked == expected_blocked, (
                    f"ImportJobStatus.{status.value} (blocked_count={blocked_count}): expected "
                    f"blocked={expected_blocked} (import_job_still_needs_raw_blob) but the SQL "
                    f"policy gave deletion_status={result.deletion_status.value} -- migration "
                    f"0020's hardcoded status list has drifted from "
                    f"app.models.import_job.import_job_still_needs_raw_blob()"
                )
    finally:
        session.rollback()
        session.close()
