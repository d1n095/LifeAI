"""app/rag/account_erasure.py::erase_account_data() and app/rag/account_export.py::
export_account_data() — Pass 26 (PR #31's account export/erasure S1A integration slice).
Real local Postgres (RLS included), same pattern as tests/backend/test_source_purge.py.
"""

import importlib.util
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from app.audit import record_audit
from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.models.audit import AuditLog
from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import (
    DocumentSourceUnit,
    LifecycleStatus,
    MemorySourceLifecycleEvent,
    MemorySourceUnit,
    SnapshotStatus,
)
from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
from app.models.user import User, UserRole
from app.rag.account_erasure import (
    AccountErasureBlockedError,
    AccountErasureResult,
    attempt_storage_deletion_task,
    claim_storage_deletion_tasks,
    erase_account_data,
)
from app.rag.account_export import EXPORT_SCHEMA_VERSION, export_account_data
from app.rag.blob_references import acquire_owner_erasure_lock
from app.rag.memory_source import DocumentSourceLocator, get_or_create_memory_source_unit
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password
from app.storage import StorageError, get_storage

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "apply_runtime_privileges.py"

# Pass 27: storage_deletion_tasks grants mainai_app INSERT only -- every read/update against
# it in these tests (mirroring app/rag/account_erasure.py's own _MaintenanceSession /
# app/worker.py's _ClaimSession) must go through this privileged connection instead of the
# ordinary SessionLocal() the rest of this file uses.
_AdminSession = sessionmaker(bind=migration_engine)


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """erase_account_data() calls erase_owner_memory(), which mainai_app is only granted
    EXECUTE on via apply_runtime_privileges.py/ensure_app_role.py's shared privilege policy —
    same rationale as tests/backend/test_source_purge.py's identical fixture."""
    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email=None, *, role=UserRole.founder) -> User:
    email = email or f"erasure-{uuid.uuid4().hex[:10]}@example.com"
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


def _version_backed_msu(session, owner_id, document_id, version) -> uuid.UUID:
    _set_rls_user(session, owner_id)
    msu_id = get_or_create_memory_source_unit(
        session,
        DocumentSourceLocator(
            owner_id=owner_id,
            document_id=document_id,
            version_id=version.id,
            chunk_id=None,
            observed_at=datetime.now(timezone.utc),
            content_text=None,
            snapshot_status=SnapshotStatus.degraded,
        ),
    )
    session.commit()
    return msu_id


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


def _store_real_blob(content: bytes) -> str:
    pos = 0

    def _read():
        nonlocal pos
        chunk = content[pos : pos + (1 << 16)]
        pos += len(chunk)
        return chunk

    blob = get_storage().write_stream(_read, max_bytes=max(len(content), 1))
    return blob.storage_key


def _make_import_job(session, owner_id, *, source_storage_key, status=ImportJobStatus.completed) -> ImportJob:
    _set_rls_user(session, owner_id)
    job = ImportJob(owner_id=owner_id, status=status, source_storage_key=source_storage_key)
    session.add(job)
    session.commit()
    return job


# --- erase_account_data(): S1A memory erasure ------------------------------------------------


def test_erase_account_data_removes_claims_dsu_msu_lifecycle_events_for_every_source_kind():
    """A single account with all three S1A source kinds (chunk/version/document_record) —
    every KnowledgeClaim, DocumentSourceUnit, MemorySourceUnit, and
    MemorySourceLifecycleEvent for the account must be gone afterward, proving
    erase_owner_memory() actually ran (not just the legacy delete_account cleanup)."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        doc_a = _make_document(session, owner.id, title="A")
        doc_b = _make_document(session, owner.id, title="B")
        doc_c = _make_document(session, owner.id, title="C")
        chunk = _make_chunk(session, owner.id, doc_a.id)
        _set_rls_user(session, owner.id)
        version = KnowledgeVersion(source_id=doc_b.id, owner_id=owner.id, version_number=1, checksum="a" * 64, extraction_version="v1")
        session.add(version)
        session.commit()

        msu_chunk = _chunk_backed_msu(session, owner.id, doc_a.id, chunk)
        msu_version = _version_backed_msu(session, owner.id, doc_b.id, version)
        msu_record = _document_record_msu(session, owner.id, doc_c.id)
        _make_claim(session, owner.id, doc_a.id, msu_chunk)

        # A lifecycle event beyond the initial "created" one, so we're not just checking the
        # find-or-create INSERT's own row survives deletion.
        _set_rls_user(session, owner.id)
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'test')"), {"id": str(msu_version)})
        session.commit()

        owner_id = owner.id
        _set_rls_user(session, owner.id)
        result = erase_account_data(session, owner)

        assert isinstance(result, AccountErasureResult)
        assert session.get(User, owner_id) is None
        for msu_id in (msu_chunk, msu_version, msu_record):
            assert session.query(MemorySourceUnit).filter_by(id=msu_id).count() == 0
            assert session.query(DocumentSourceUnit).filter_by(memory_source_id=msu_id).count() == 0
            assert session.query(MemorySourceLifecycleEvent).filter_by(memory_source_id=msu_id).count() == 0
        assert session.query(KnowledgeClaim).filter_by(owner_id=owner_id).count() == 0
        assert session.query(Document).filter_by(uploaded_by=owner_id).count() == 0
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_works_for_a_legacy_account_with_no_memory_source_units():
    """A pre-S1A account (documents/claims but zero MemorySourceUnit rows, never backfilled)
    must still erase cleanly — erase_owner_memory() deleting zero rows is not an error."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        owner_id = owner.id
        _set_rls_user(session, owner.id)

        result = erase_account_data(session, owner)

        assert session.get(User, owner_id) is None
        assert result.storage_tasks_created == 0
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_rolls_back_everything_on_a_failure_after_erase_owner_memory(monkeypatch):
    """A failure AFTER erase_owner_memory() has already run (but before the final commit) must
    roll back the S1A erasure too — not just the later Python deletes."""
    from sqlalchemy.orm import Session as SASession

    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        msu_id = _chunk_backed_msu(session, owner.id, document.id, chunk)
        owner_id = owner.id

        original_delete = SASession.delete

        def _failing_delete(self, instance):
            if isinstance(instance, User):
                raise RuntimeError("simulated failure deleting the User row")
            return original_delete(self, instance)

        monkeypatch.setattr(SASession, "delete", _failing_delete)

        _set_rls_user(session, owner.id)
        with pytest.raises(RuntimeError):
            erase_account_data(session, owner)

        monkeypatch.undo()

        assert session.get(User, owner_id) is not None
        assert session.query(MemorySourceUnit).filter_by(id=msu_id).count() == 1
        assert session.query(Document).filter_by(id=document.id).count() == 1
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_rolls_back_everything_when_a_storage_deletion_task_insert_fails(monkeypatch):
    """A real Postgres constraint violation while inserting a storage_deletion_tasks row (here,
    a deliberately invalid attempt_count) must roll back the ENTIRE erasure — S1A memory,
    documents, and the User row all survive, exactly as if nothing had been attempted."""
    import app.rag.account_erasure as account_erasure_module
    from app.models.storage_deletion_task import StorageDeletionTask as _RealTask

    class _BrokenStorageDeletionTask(_RealTask):
        def __init__(self, *args, **kwargs):
            kwargs["attempt_count"] = -1  # violates ck_storage_deletion_tasks_attempt_count
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(account_erasure_module, "StorageDeletionTask", _BrokenStorageDeletionTask)

    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"task insert failure proof")
        document = _make_document(session, owner.id, storage_key=storage_key)
        owner_id = owner.id

        _set_rls_user(session, owner.id)
        with pytest.raises(Exception):
            erase_account_data(session, owner)

        session.rollback()
        assert session.get(User, owner_id) is not None
        assert session.query(Document).filter_by(id=document.id).count() == 1
        admin = _AdminSession()
        try:
            assert admin.query(StorageDeletionTask).count() == 0
        finally:
            admin.close()
        assert get_storage().exists(storage_key)  # never even attempted, let alone deleted
    finally:
        session.rollback()
        session.close()


# --- storage_deletion_tasks: creation, dedup, and physical blob outcomes --------------------


def test_erase_account_data_deduplicates_document_and_import_job_keys_into_one_task_per_key():
    """The SAME storage_key referenced by both a Document AND an ImportJob for this account
    must produce exactly ONE storage_deletion_tasks row, not two."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"shared between a document and its own import job")
        _make_document(session, owner.id, storage_key=storage_key)
        _make_import_job(session, owner.id, source_storage_key=storage_key)
        owner_id = owner.id

        _set_rls_user(session, owner.id)
        result = erase_account_data(session, owner)

        assert result.storage_tasks_created == 1
        admin = _AdminSession()
        try:
            tasks = admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).all()
            assert len(tasks) == 1
            assert tasks[0].storage_key == storage_key
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_covers_both_document_storage_key_and_import_job_source_storage_key():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        doc_key = _store_real_blob(b"document-only blob")
        job_key = _store_real_blob(b"import-job-only blob")
        _make_document(session, owner.id, storage_key=doc_key)
        _make_import_job(session, owner.id, source_storage_key=job_key)

        _set_rls_user(session, owner.id)
        result = erase_account_data(session, owner)

        admin = _AdminSession()
        try:
            tasks = admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).all()
            assert {t.storage_key for t in tasks} == {doc_key, job_key}
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_purges_an_unshared_blob_immediately():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"nobody else needs this")
        _make_document(session, owner.id, storage_key=storage_key)

        _set_rls_user(session, owner.id)
        result = erase_account_data(session, owner)

        assert result.storage_tasks_purged_immediately == 1
        assert not get_storage().exists(storage_key)
        admin = _AdminSession()
        try:
            task = admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).one()
            assert task.status == StorageDeletionStatus.purged
            assert task.completed_at is not None
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_retains_a_blob_still_referenced_by_another_owner():
    """The core cross-owner guarantee: owner A's erasure must NOT physically delete a blob
    owner B's still-live Document depends on — storage_key_still_referenced_global() (migration
    0020) is what the task-attempt logic defers to, exactly like source_purge.py's Phase B."""
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email=f"erase-shared-a-{uuid.uuid4().hex[:8]}@example.com")
        owner_b = _make_user(session, email=f"erase-shared-b-{uuid.uuid4().hex[:8]}@example.com")
        storage_key = _store_real_blob(b"content shared byte-for-byte across two owners")
        _make_document(session, owner_a.id, storage_key=storage_key)
        _make_document(session, owner_b.id, storage_key=storage_key)

        _set_rls_user(session, owner_a.id)
        result = erase_account_data(session, owner_a)

        assert result.storage_tasks_retained_shared_immediately == 1
        assert get_storage().exists(storage_key)  # untouched -- owner B still needs it
        admin = _AdminSession()
        try:
            task = admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).one()
            assert task.status == StorageDeletionStatus.retained_shared
        finally:
            admin.close()

        # Owner B's own document is completely unaffected -- re-scope RLS to owner B first, or
        # this query would be silently (and misleadingly) filtered down to zero rows by RLS
        # itself, still bound to owner A's context from the erasure call above.
        _set_rls_user(session, owner_b.id)
        assert session.query(Document).filter_by(uploaded_by=owner_b.id, storage_key=storage_key).count() == 1
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_never_calls_storage_delete_before_the_db_transaction_commits(monkeypatch):
    """Forces the DB phase to fail AFTER a storage_deletion_tasks row is queued but before
    commit — the blob must still be fully intact afterward, proving no physical delete can
    ever happen ahead of a successful commit (mirrors app/rag/source_purge.py's own guarantee
    for single-source purges)."""
    from sqlalchemy.orm import Session as SASession

    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"must survive a mid-transaction failure untouched")
        _make_document(session, owner.id, storage_key=storage_key)

        original_delete = SASession.delete

        def _failing_delete(self, instance):
            if isinstance(instance, User):
                raise RuntimeError("simulated failure")
            return original_delete(self, instance)

        monkeypatch.setattr(SASession, "delete", _failing_delete)
        _set_rls_user(session, owner.id)
        with pytest.raises(RuntimeError):
            erase_account_data(session, owner)
        monkeypatch.undo()

        assert get_storage().exists(storage_key)
        admin = _AdminSession()
        try:
            assert admin.query(StorageDeletionTask).count() == 0  # rolled back along with everything else
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_attempt_storage_deletion_task_marks_a_real_storage_error_as_failed_and_retry_succeeds(monkeypatch):
    """A transient storage I/O error is recorded as `failed` (retryable), not silently
    swallowed or mis-marked `purged` — and a later, unpatched retry of the SAME task reaches
    `purged` cleanly, proving the task genuinely survives to be retried.

    Pass 27: runs entirely on `_AdminSession` -- `attempt_storage_deletion_task` writes
    (status/attempt_count) require UPDATE, which mainai_app no longer has on this table (see
    module docstring)."""
    session = _AdminSession()
    try:
        storage_key = _store_real_blob(b"flaky storage backend proof")
        task = StorageDeletionTask(operation_id=uuid.uuid4(), storage_key=storage_key, status=StorageDeletionStatus.pending)
        session.add(task)
        session.commit()

        import app.rag.account_erasure as account_erasure_module

        real_storage = get_storage()

        class _FlakyStorage:
            def delete(self, key):
                raise StorageError("simulated transient I/O failure")

            def exists(self, key):
                return real_storage.exists(key)

        monkeypatch.setattr(account_erasure_module, "get_storage", lambda: _FlakyStorage())
        attempt_storage_deletion_task(session, task)
        assert task.status == StorageDeletionStatus.failed
        assert task.last_error is not None
        assert task.attempt_count == 1
        assert get_storage().exists(storage_key)  # never actually deleted
        monkeypatch.undo()

        # Retry with the real backend picks the SAME task back up and finishes the job.
        task = session.query(StorageDeletionTask).filter_by(id=task.id).one()
        attempt_storage_deletion_task(session, task)
        assert task.status == StorageDeletionStatus.purged
        assert task.attempt_count == 2
        assert not get_storage().exists(storage_key)
    finally:
        session.rollback()
        session.close()


def test_attempt_storage_deletion_task_is_idempotent_on_a_key_already_deleted_from_disk():
    """A successful physical delete followed by a status-commit failure (simulated here by
    deleting the file out-of-band first) must not error on retry — LocalFilesystemStorage's
    own unlink(missing_ok=True) makes a second delete attempt a clean no-op."""
    session = _AdminSession()
    try:
        storage_key = _store_real_blob(b"already gone by the time we retry")
        get_storage().delete(storage_key)  # simulates: delete succeeded, but status-commit didn't
        task = StorageDeletionTask(operation_id=uuid.uuid4(), storage_key=storage_key, status=StorageDeletionStatus.pending)
        session.add(task)
        session.commit()

        attempt_storage_deletion_task(session, task)

        assert task.status == StorageDeletionStatus.purged
    finally:
        session.rollback()
        session.close()


def test_attempt_storage_deletion_task_is_a_no_op_for_an_already_terminal_task():
    session = _AdminSession()
    try:
        storage_key = _store_real_blob(b"already terminal, must not be re-attempted")
        task = StorageDeletionTask(
            operation_id=uuid.uuid4(), storage_key=storage_key, status=StorageDeletionStatus.retained_shared
        )
        session.add(task)
        session.commit()
        attempt_count_before = task.attempt_count

        attempt_storage_deletion_task(session, task)

        assert task.attempt_count == attempt_count_before  # untouched
        assert get_storage().exists(storage_key)  # a retained_shared blob is never deleted
    finally:
        session.rollback()
        session.close()


# --- export_account_data() -------------------------------------------------------------------


def test_export_account_data_includes_active_revoked_and_purged_sources_with_correct_content():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        doc = _make_document(session, owner.id)
        chunk_active = _make_chunk(session, owner.id, doc.id, text_value="active chunk")
        chunk_revoked = DocumentChunk(document_id=doc.id, owner_id=owner.id, chunk_index=1, text="revoked chunk", embedding=[0.1] * 1536)
        chunk_purged = DocumentChunk(document_id=doc.id, owner_id=owner.id, chunk_index=2, text="purged chunk", embedding=[0.1] * 1536)
        _set_rls_user(session, owner.id)
        session.add_all([chunk_revoked, chunk_purged])
        session.commit()

        msu_active = _chunk_backed_msu(session, owner.id, doc.id, chunk_active)
        msu_revoked = _chunk_backed_msu(session, owner.id, doc.id, chunk_revoked)
        msu_purged = _chunk_backed_msu(session, owner.id, doc.id, chunk_purged)

        _set_rls_user(session, owner.id)
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'export test')"), {"id": str(msu_revoked)})
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'purged', 'export test')"), {"id": str(msu_purged)})
        session.commit()

        _set_rls_user(session, owner.id)
        export = export_account_data(session, owner)

        by_id = {m["id"]: m for m in export["memory_source_units"]}
        assert by_id[str(msu_active)]["lifecycle_status"] == "active"
        assert by_id[str(msu_active)]["content_text"] == "active chunk"
        assert by_id[str(msu_revoked)]["lifecycle_status"] == "revoked"
        assert by_id[str(msu_revoked)]["content_text"] == "revoked chunk"  # snapshot kept, reversible
        assert by_id[str(msu_purged)]["lifecycle_status"] == "purged"
        # Never fabricated -- a purged source's content is genuinely gone in the DB, and the
        # export must faithfully reflect that, not reconstruct it from elsewhere.
        assert by_id[str(msu_purged)]["content_text"] is None
        assert by_id[str(msu_purged)]["content_hash"] is None
    finally:
        session.rollback()
        session.close()


def test_export_account_data_includes_document_source_units_and_lifecycle_events():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        doc = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, doc.id)
        msu_id = _chunk_backed_msu(session, owner.id, doc.id, chunk)

        _set_rls_user(session, owner.id)
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'r1')"), {"id": str(msu_id)})
        session.execute(sa_text("SELECT transition_own_memory_source(:id, 'active', 'r2')"), {"id": str(msu_id)})
        session.commit()

        _set_rls_user(session, owner.id)
        export = export_account_data(session, owner)

        dsu = next(d for d in export["document_source_units"] if d["memory_source_id"] == str(msu_id))
        assert dsu["document_id"] == str(doc.id)
        assert dsu["chunk_id"] == str(chunk.id)

        events = [e for e in export["memory_source_lifecycle_events"] if e["memory_source_id"] == str(msu_id)]
        transitions = [(e["from_status"], e["to_status"]) for e in events]
        assert ("active", "revoked") in transitions
        assert ("revoked", "active") in transitions
    finally:
        session.rollback()
        session.close()


def test_export_account_data_includes_claims_linked_to_memory_source_id_and_legacy_fields():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        doc = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, doc.id)
        msu_id = _chunk_backed_msu(session, owner.id, doc.id, chunk)
        claim = _make_claim(session, owner.id, doc.id, msu_id, claim_text="Ett exporterat pastaende.")

        _set_rls_user(session, owner.id)
        export = export_account_data(session, owner)

        claim_out = next(c for c in export["knowledge_claims"] if c["id"] == str(claim.id))
        assert claim_out["memory_source_id"] == str(msu_id)
        assert claim_out["source_id"] == str(doc.id)
        assert claim_out["claim_text"] == "Ett exporterat pastaende."
    finally:
        session.rollback()
        session.close()


def test_export_account_data_never_includes_another_owners_s1a_data():
    session = SessionLocal()
    try:
        owner = _make_user(session, email=f"export-iso-a-{uuid.uuid4().hex[:8]}@example.com")
        other = _make_user(session, email=f"export-iso-b-{uuid.uuid4().hex[:8]}@example.com")
        my_doc = _make_document(session, owner.id, title="mine")
        other_doc = _make_document(session, other.id, title="not mine")
        my_chunk = _make_chunk(session, owner.id, my_doc.id)
        other_chunk = _make_chunk(session, other.id, other_doc.id)
        my_msu = _chunk_backed_msu(session, owner.id, my_doc.id, my_chunk)
        other_msu = _chunk_backed_msu(session, other.id, other_doc.id, other_chunk)
        _make_claim(session, owner.id, my_doc.id, my_msu)
        _make_claim(session, other.id, other_doc.id, other_msu)

        _set_rls_user(session, owner.id)
        export = export_account_data(session, owner)

        msu_ids = {m["id"] for m in export["memory_source_units"]}
        claim_source_ids = {c["memory_source_id"] for c in export["knowledge_claims"]}
        assert str(my_msu) in msu_ids
        assert str(other_msu) not in msu_ids
        assert str(other_msu) not in claim_source_ids
    finally:
        session.rollback()
        session.close()


def test_export_account_data_has_schema_version_and_deterministic_ordering():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        doc = _make_document(session, owner.id)
        for i in range(3):
            _make_chunk(session, owner.id, doc.id, text_value=f"chunk {i}")

        _set_rls_user(session, owner.id)
        export_1 = export_account_data(session, owner)
        session.commit()
        _set_rls_user(session, owner.id)
        export_2 = export_account_data(session, owner)

        assert export_1["export_schema_version"] == EXPORT_SCHEMA_VERSION
        assert "generated_at" in export_1
        # Ordering of a stable data set must be reproducible across two separate calls.
        assert [d["id"] for d in export_1["knowledge_sources"]] == [d["id"] for d in export_2["knowledge_sources"]]
    finally:
        session.rollback()
        session.close()


def test_export_account_data_writes_exactly_one_audit_entry():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        _set_rls_user(session, owner.id)

        export_account_data(session, owner)

        count = session.query(AuditLog).filter_by(user_id=owner_id, action="account_data_exported").count()
        assert count == 1
    finally:
        session.rollback()
        session.close()


def test_export_account_data_failure_produces_no_audit_entry(monkeypatch):
    """If assembling the export raises partway through, no `account_data_exported` audit row
    may exist afterward -- a failed export must never look, in the audit trail, like data was
    actually returned to the caller."""
    import app.rag.account_export as account_export_module

    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        _make_document(session, owner.id)

        real_query = session.query

        def _raising_query(model, *a, **kw):
            if model is account_export_module.KnowledgeClaim:
                raise RuntimeError("simulated failure assembling the export")
            return real_query(model, *a, **kw)

        monkeypatch.setattr(session, "query", _raising_query)

        _set_rls_user(session, owner.id)
        with pytest.raises(RuntimeError):
            export_account_data(session, owner)

        monkeypatch.undo()
        count = session.query(AuditLog).filter_by(user_id=owner_id, action="account_data_exported").count()
        assert count == 0
    finally:
        session.rollback()
        session.close()


# --- the erasure/upload lock race -------------------------------------------------------------


def test_owner_erasure_lock_serializes_erasure_against_a_concurrent_upload_for_the_same_owner():
    """Reproduces the exact race a founder review found, with two REAL, separate DB sessions
    and a real Postgres advisory lock (acquire_owner_erasure_lock) -- not a mocked timer. An
    "upload" thread acquires the lock first (mirroring app/routers/library.py's import_package
    acquiring it before storage.write_stream) and holds it while it commits a fresh ImportJob
    referencing a brand-new storage_key; only after it commits (releasing the lock) does the
    "erasure" thread's own acquire unblock -- at which point erase_account_data's storage-key
    inventory MUST see the just-committed ImportJob's key and create a deletion task for it."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        session.commit()
    finally:
        session.close()

    lock_acquired = threading.Event()
    proceed_to_commit = threading.Event()
    upload_committed = threading.Event()
    new_storage_key = _store_real_blob(b"written by the racing upload thread")

    def _upload_thread():
        upload_session = SessionLocal()
        try:
            acquire_owner_erasure_lock(upload_session, owner_id)
            lock_acquired.set()
            proceed_to_commit.wait(timeout=5)
            _set_rls_user(upload_session, owner_id)
            upload_session.add(ImportJob(owner_id=owner_id, status=ImportJobStatus.completed, source_storage_key=new_storage_key))
            upload_session.commit()  # releases the advisory lock
        finally:
            upload_committed.set()
            upload_session.close()

    upload_thread = threading.Thread(target=_upload_thread)
    upload_thread.start()
    assert lock_acquired.wait(timeout=5), "upload thread never acquired the owner-erasure lock"

    erasure_session = SessionLocal()
    try:
        proceed_to_commit.set()
        _set_rls_user(erasure_session, owner_id)
        # Blocks on the SAME advisory lock (a different connection) until the upload thread's
        # transaction above commits -- the exact serialization point being tested.
        owner_row = erasure_session.query(User).filter_by(id=owner_id).first()
        result = erase_account_data(erasure_session, owner_row)
        upload_thread.join(timeout=5)

        assert upload_committed.is_set()
        admin = _AdminSession()
        try:
            task_keys = {
                t.storage_key for t in admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).all()
            }
        finally:
            admin.close()
        assert new_storage_key in task_keys, (
            "the upload committed before erasure's storage-key inventory ran, so its fresh "
            "ImportJob.source_storage_key MUST have been swept into a deletion task -- an "
            "orphaned, DB-unreferenced blob is exactly the bug this lock exists to prevent"
        )
    finally:
        erasure_session.rollback()
        erasure_session.close()


# --- storage_deletion_tasks: privilege boundary (Pass 27, founder review round 2) -----------
#
# The table-wide catalog checks (mainai_app has exactly INSERT, nothing else) live in
# tests/backend/test_memory_source_units.py::test_mainai_app_privileges_are_exactly_least_
# privilege_no_truncate_references_trigger and ::test_apply_runtime_privileges_survives_a_
# second_boot (both extended in Pass 27 to cover storage_deletion_tasks). The tests below prove
# the SAME thing at RUNTIME, through this session's actual mainai_app connection -- not just
# the catalog's opinion of what it SHOULD be able to do.


def test_mainai_app_session_cannot_select_from_storage_deletion_tasks():
    """This session already IS mainai_app (app/db.py's `engine` binds APP_DATABASE_URL) -- a
    permission error here is the real enforcement, not a mock."""
    session = SessionLocal()
    try:
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.query(StorageDeletionTask).count()
        assert "permission denied" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


def test_mainai_app_session_cannot_update_storage_deletion_tasks():
    session = SessionLocal()
    try:
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(sa_text("UPDATE storage_deletion_tasks SET status = 'purged' WHERE true"))
            session.commit()
        assert "permission denied" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


def test_mainai_app_session_can_still_insert_storage_deletion_tasks():
    """The one privilege the account-erasure transaction actually needs -- proven positively,
    not just by absence of the tests above."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        session.add(
            StorageDeletionTask(operation_id=uuid.uuid4(), storage_key=f"insert-only-proof-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending)
        )
        session.commit()  # must not raise
    finally:
        session.rollback()
        session.close()


# --- claim_storage_deletion_tasks(): atomic multi-worker claiming (Pass 27) ------------------


def test_claim_storage_deletion_tasks_claims_pending_and_failed_scoped_to_operation_id():
    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        other_op_id = uuid.uuid4()
        t_pending = StorageDeletionTask(operation_id=op_id, storage_key=f"claim-pending-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending)
        t_failed = StorageDeletionTask(operation_id=op_id, storage_key=f"claim-failed-{uuid.uuid4().hex}", status=StorageDeletionStatus.failed)
        t_other_op = StorageDeletionTask(operation_id=other_op_id, storage_key=f"claim-other-op-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending)
        admin.add_all([t_pending, t_failed, t_other_op])
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10, operation_id=op_id)

        assert set(claimed) == {t_pending.id, t_failed.id}
        admin.expire_all()
        assert admin.get(StorageDeletionTask, t_pending.id).status == StorageDeletionStatus.processing
        assert admin.get(StorageDeletionTask, t_failed.id).status == StorageDeletionStatus.processing
        # A different operation's own pending task is untouched by this operation-scoped claim.
        assert admin.get(StorageDeletionTask, t_other_op.id).status == StorageDeletionStatus.pending
    finally:
        admin.rollback()
        admin.close()


def test_claim_storage_deletion_tasks_respects_the_limit_bound():
    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        for i in range(5):
            admin.add(StorageDeletionTask(operation_id=op_id, storage_key=f"claim-limit-{i}-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending))
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=2, operation_id=op_id)
        assert len(claimed) == 2
    finally:
        admin.rollback()
        admin.close()


def test_claim_storage_deletion_tasks_does_not_reclaim_a_processing_task_still_within_its_lease():
    admin = _AdminSession()
    try:
        task = StorageDeletionTask(operation_id=uuid.uuid4(), storage_key=f"still-leased-{uuid.uuid4().hex}", status=StorageDeletionStatus.processing)
        admin.add(task)
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10, lease_seconds=300)

        assert task.id not in claimed, "a task claimed moments ago (fresh updated_at) must not be reclaimable within its lease"
    finally:
        admin.rollback()
        admin.close()


def test_claim_storage_deletion_tasks_reclaims_a_processing_task_after_its_lease_expires():
    """Simulates a claimer that crashed mid-attempt: a `processing` row whose `updated_at` is
    older than the lease window must become claimable again -- the same "abandoned claim"
    shape app/jobs/lease.py's claim_next_job() already handles for knowledge_import_jobs via
    lease_expires_at."""
    admin = _AdminSession()
    try:
        task = StorageDeletionTask(operation_id=uuid.uuid4(), storage_key=f"stuck-processing-{uuid.uuid4().hex}", status=StorageDeletionStatus.processing)
        admin.add(task)
        admin.commit()
        task_id = task.id
        admin.execute(
            sa_text("UPDATE storage_deletion_tasks SET updated_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": str(task_id)},
        )
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10, lease_seconds=60)

        assert task_id in claimed
    finally:
        admin.rollback()
        admin.close()


def test_claim_storage_deletion_tasks_two_concurrent_claimers_never_claim_the_same_task():
    """Real two-thread, two-connection reproduction: N tasks, two callers racing to claim the
    same operation's tasks at (as close as possible to) the same instant. FOR UPDATE SKIP
    LOCKED must guarantee every task is claimed by exactly one caller, never both."""
    task_count = 20
    op_id = uuid.uuid4()
    all_ids: set[uuid.UUID] = set()
    admin = _AdminSession()
    try:
        for i in range(task_count):
            t = StorageDeletionTask(operation_id=op_id, storage_key=f"race-{i}-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending)
            admin.add(t)
            admin.flush()
            all_ids.add(t.id)
        admin.commit()
    finally:
        admin.close()

    results: dict[str, list] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    def _claim(key: str) -> None:
        session = _AdminSession()
        try:
            barrier.wait(timeout=5)
            results[key] = claim_storage_deletion_tasks(session, limit=task_count, operation_id=op_id)
        finally:
            session.close()

    t_a = threading.Thread(target=_claim, args=("a",))
    t_b = threading.Thread(target=_claim, args=("b",))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    claimed_a = set(results["a"])
    claimed_b = set(results["b"])
    assert claimed_a.isdisjoint(claimed_b), "the same task was claimed by BOTH concurrent callers"
    assert claimed_a | claimed_b == all_ids, "every task must be claimed by exactly one of the two callers"


# --- erase_account_data(): refuses while a worker is actively processing this owner's import
# (Pass 27, blob-write-path audit) -------------------------------------------------------------


def test_erase_account_data_raises_blocked_error_while_an_import_job_is_actively_running():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        job = ImportJob(owner_id=owner.id, status=ImportJobStatus.running, source_storage_key=None)
        session.add(job)
        session.commit()
        session.execute(
            sa_text("UPDATE knowledge_import_jobs SET lease_expires_at = now() + interval '1 hour' WHERE id = :id"),
            {"id": str(job.id)},
        )
        session.commit()

        _set_rls_user(session, owner.id)
        with pytest.raises(AccountErasureBlockedError):
            erase_account_data(session, owner)

        # Nothing was touched -- the account must still exist, exactly as if erasure was never
        # attempted at all.
        assert session.get(User, owner.id) is not None
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_proceeds_when_an_import_job_is_only_pending_not_running():
    """A queued-but-not-yet-claimed job has no worker actively writing blobs for it right
    now -- erasure must not be blocked by it (see module docstring's documented residual
    race for the narrow window this doesn't fully close)."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        _set_rls_user(session, owner.id)
        session.add(ImportJob(owner_id=owner.id, status=ImportJobStatus.pending, source_storage_key=None))
        session.commit()

        _set_rls_user(session, owner.id)
        erase_account_data(session, owner)  # must not raise

        assert session.get(User, owner_id) is None
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_proceeds_when_a_running_jobs_lease_has_already_expired():
    """A `running` job whose lease already expired is an ABANDONED claim (the exact same
    signal claim_next_job uses to reclaim it) -- not a genuinely active worker, so it must not
    block erasure."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        _set_rls_user(session, owner.id)
        job = ImportJob(owner_id=owner.id, status=ImportJobStatus.running, source_storage_key=None)
        session.add(job)
        session.commit()
        session.execute(
            sa_text("UPDATE knowledge_import_jobs SET lease_expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": str(job.id)},
        )
        session.commit()

        _set_rls_user(session, owner.id)
        erase_account_data(session, owner)  # must not raise

        assert session.get(User, owner_id) is None
    finally:
        session.rollback()
        session.close()


# --- export_account_data(): controlled audit-commit transaction (Pass 27) -------------------


def test_export_account_data_rolls_back_and_raises_when_the_final_commit_fails(monkeypatch):
    """A forced failure on THIS function's own explicit db.commit() (after the audit row was
    added with commit=False) must roll the audit insert back and re-raise -- never return the
    export as if it had succeeded, and never leave a half-committed audit row behind."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        _set_rls_user(session, owner.id)

        call_count = {"n": 0}

        def _failing_commit():
            call_count["n"] += 1
            raise RuntimeError("simulated commit failure after audit insert")

        # Only the export call below must see the failing commit -- setup above (_make_user's
        # own commit) already ran on the real one.
        monkeypatch.setattr(session, "commit", _failing_commit)

        with pytest.raises(RuntimeError):
            export_account_data(session, owner)

        assert call_count["n"] == 1
        monkeypatch.undo()
        session.rollback()

        count = session.query(AuditLog).filter_by(user_id=owner_id, action="account_data_exported").count()
        assert count == 0
    finally:
        session.rollback()
        session.close()


# --- StorageDeletionTask model / migration 0021 schema agreement (Pass 27) ------------------


def test_storage_deletion_tasks_reason_and_status_columns_are_plain_varchar_not_native_enum():
    """Migration 0021 defines these as `varchar(N) + CHECK`, never `CREATE TYPE ... AS ENUM`
    -- the model must describe the SAME real schema (native_enum=False on both columns), not
    a native Postgres enum type that was never actually created."""
    session = SessionLocal()
    try:
        rows = {
            row[0]: row[1]
            for row in session.execute(
                sa_text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'storage_deletion_tasks' "
                    "AND column_name IN ('reason', 'status')"
                )
            ).all()
        }
        assert rows == {"reason": "character varying", "status": "character varying"}

        enum_type_count = session.execute(
            sa_text("SELECT count(*) FROM pg_type WHERE typname IN ('storagedeletionreason', 'storagedeletionstatus')")
        ).scalar()
        assert enum_type_count == 0, "no native Postgres ENUM TYPE should exist for these columns"
    finally:
        session.rollback()
        session.close()


def test_storage_deletion_tasks_check_constraint_rejects_an_invalid_status():
    """The migration's CHECK constraint is the real database truth (see model docstring) --
    proven directly by attempting to insert a value outside the allowed set."""
    admin = _AdminSession()
    try:
        with pytest.raises(IntegrityError):
            admin.execute(
                sa_text(
                    "INSERT INTO storage_deletion_tasks (id, operation_id, storage_key, status) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'bad-status-proof', 'bogus')"
                )
            )
            admin.commit()
    finally:
        admin.rollback()
        admin.close()
