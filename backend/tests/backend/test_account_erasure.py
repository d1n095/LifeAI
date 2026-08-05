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
from app.jobs.lease import claim_next_job
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
from app.models.project_memory import ProjectCheckpoint, ProjectSource
from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
from app.models.user import User, UserRole
from app.rag.account_erasure import (
    AccountErasureBlockedError,
    AccountErasureResult,
    attempt_pending_storage_deletions_for_operation,
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
    same rationale as tests/backend/test_source_purge.py's identical fixture.

    Integration (mainai-job-runtime): erase_account_data() now ALSO calls
    erase_own_mainai_job_children() (migration 0027) — a completely separate SECURITY DEFINER
    function governed by app/rls.py's apply_mainai_job_runtime_privileges(), not by
    scripts/s1a_privilege_policy.py above. Production's real boot sequence (app/main.py's
    on_startup) always calls both; this fixture must too, or mainai_app has no EXECUTE on
    erase_own_mainai_job_children() in this module's tests and every erase_account_data() call
    here fails with a permission error before any of its own assertions can run."""
    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)
    from app.rls import apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)


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
    """A real failure calling `enqueue_account_erasure_storage_task()` (here, a simulated
    Postgres error) must roll back the ENTIRE erasure — S1A memory, documents, and the User
    row all survive, exactly as if nothing had been attempted. Pass 28: this call is now a
    `SELECT enqueue_account_erasure_storage_task(...)` (SECURITY DEFINER function), not a
    plain ORM insert (see app/rag/account_erasure.py's module docstring) — the failure is
    simulated by intercepting exactly that statement text on the real session's `execute`,
    not by breaking the ORM model."""
    from sqlalchemy.orm import Session as SASession

    original_execute = SASession.execute

    def _failing_execute(self, statement, *args, **kwargs):
        if "enqueue_account_erasure_storage_task" in str(statement):
            raise RuntimeError("simulated failure enqueueing a storage_deletion_task")
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(SASession, "execute", _failing_execute)

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


# --- Pass 29: cross-domain blob retention (Project Memory shares the SAME content-addressed
# storage backend as Document/ImportJob) --------------------------------------------------
#
# A founder review found migration 0020's storage_key_still_referenced_global() blind to
# app/project_memory.py's founder-wide ProjectSource.storage_key/ProjectCheckpoint.
# brief_storage_key -- both write through the exact same get_storage()/write_stream() backend,
# so a byte-identical upload from an ordinary account could share a storage_key with Project
# Memory content, and that account's own erasure would previously have physically deleted the
# shared blob out from under Project Memory (migration 0023 fixes the SQL function itself; see
# test_source_purge.py's Pass 29 section for the direct SQL-function tests C/D). Tests A/B
# below are the founder's own lettering: the end-to-end proof through a REAL erase_account_data()
# call, not just the SQL function in isolation.


def test_erase_account_data_retains_a_blob_still_referenced_by_a_project_source():
    """Test A (founder's lettering): a ProjectSource sharing the same content-addressed key as
    an erased owner's Document must survive the erasure untouched -- the blob is retained_
    shared, never physically deleted, and the ProjectSource row itself is never touched (this
    domain isn't even visible to account erasure's own queries, only to the SQL reference
    check the physical-delete decision defers to)."""
    session = SessionLocal()
    try:
        owner = _make_user(session, email=f"erase-pm-source-{uuid.uuid4().hex[:8]}@example.com")
        storage_key = _store_real_blob(b"pass 29: shared between a user Document and ProjectSource")
        _make_document(session, owner.id, storage_key=storage_key)

        admin = _AdminSession()
        try:
            project_source = ProjectSource(source_type="doc", source_ref="docs/EXAMPLE.md", storage_key=storage_key, ingested_by="test")
            admin.add(project_source)
            admin.commit()
            project_source_id = project_source.id
        finally:
            admin.close()

        _set_rls_user(session, owner.id)
        result = erase_account_data(session, owner)

        assert result.storage_tasks_retained_shared_immediately == 1
        assert get_storage().exists(storage_key)  # untouched -- Project Memory still needs it
        admin = _AdminSession()
        try:
            task = admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).one()
            assert task.status == StorageDeletionStatus.retained_shared
            still_there = admin.query(ProjectSource).filter_by(id=project_source_id).one()
            assert still_there.storage_key == storage_key
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_erase_account_data_retains_a_blob_still_referenced_by_a_project_checkpoint():
    """Test B (founder's lettering): same proof for ProjectCheckpoint.brief_storage_key."""
    session = SessionLocal()
    try:
        owner = _make_user(session, email=f"erase-pm-checkpoint-{uuid.uuid4().hex[:8]}@example.com")
        storage_key = _store_real_blob(b"pass 29: shared between a user Document and a checkpoint brief")
        _make_document(session, owner.id, storage_key=storage_key)

        admin = _AdminSession()
        try:
            checkpoint = ProjectCheckpoint(
                summary="test", branch_name="main", open_pr_refs="", brief_storage_key=storage_key, brief_sha256="a" * 64, created_by="test"
            )
            admin.add(checkpoint)
            admin.commit()
            checkpoint_id = checkpoint.id
        finally:
            admin.close()

        _set_rls_user(session, owner.id)
        result = erase_account_data(session, owner)

        assert result.storage_tasks_retained_shared_immediately == 1
        assert get_storage().exists(storage_key)
        admin = _AdminSession()
        try:
            task = admin.query(StorageDeletionTask).filter_by(operation_id=result.operation_id).one()
            assert task.status == StorageDeletionStatus.retained_shared
            still_there = admin.query(ProjectCheckpoint).filter_by(id=checkpoint_id).one()
            assert still_there.brief_storage_key == storage_key
        finally:
            admin.close()
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
        # Pass 28: a failed attempt sets a future backoff -- the worker's own retry scan
        # (claim_storage_deletion_tasks with the default include_failed=True) must not
        # reclaim this task again until that time has passed.
        assert task.next_attempt_at is not None
        assert task.next_attempt_at > datetime.now(timezone.utc)
        assert get_storage().exists(storage_key)  # never actually deleted
        monkeypatch.undo()

        # Retry with the real backend picks the SAME task back up and finishes the job.
        task = session.query(StorageDeletionTask).filter_by(id=task.id).one()
        attempt_storage_deletion_task(session, task)
        assert task.status == StorageDeletionStatus.purged
        assert task.attempt_count == 2
        assert task.next_attempt_at is None  # reset the moment a fresh attempt started
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


# --- claim_next_job(): two-phase owner-locked claim closes the pending-ImportJob/account-
# erasure orphan-blob race (Pass 28, founder review round 3) ---------------------------------


def test_claim_next_job_winning_the_owner_lock_race_blocks_a_concurrent_erasure(monkeypatch):
    """Reproduces the exact orphan-blob race the founder review found, with claim_next_job
    winning the owner-lock race for a PENDING job. Proves the required lock ordering (owner
    lock acquired BEFORE the row-level claim, never after, app/jobs/lease.py's module
    docstring): a concurrent erasure for the SAME owner must block on the SAME real Postgres
    advisory lock until claim's transaction fully commits, and once unblocked must find the
    job already claimed/running -- refusing to erase out from under an active worker
    (AccountErasureBlockedError), never racing to delete it. Also a deadlock-timeout proof:
    both threads complete within their bounded joins below, never hanging."""
    import app.jobs.lease as lease_module

    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        storage_key = _store_real_blob(b"claim wins the owner-lock race")
        _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.pending)
    finally:
        session.close()

    lock_acquired = threading.Event()
    proceed_to_claim = threading.Event()
    claim_done = threading.Event()
    real_acquire = lease_module.acquire_owner_erasure_lock

    def _pausing_acquire(db, owner_id_arg):
        real_acquire(db, owner_id_arg)
        lock_acquired.set()
        proceed_to_claim.wait(timeout=5)

    monkeypatch.setattr(lease_module, "acquire_owner_erasure_lock", _pausing_acquire)

    claim_result: dict = {}

    def _claim_thread():
        claim_db = _AdminSession()
        try:
            claim_result["value"] = claim_next_job(claim_db, "race-worker-claim-wins", 120)
        finally:
            claim_done.set()
            claim_db.close()

    t = threading.Thread(target=_claim_thread)
    t.start()
    try:
        assert lock_acquired.wait(timeout=5), "claim_next_job never acquired the owner-erasure lock"

        erasure_session = SessionLocal()
        try:
            proceed_to_claim.set()
            _set_rls_user(erasure_session, owner_id)
            owner_row = erasure_session.query(User).filter_by(id=owner_id).first()
            with pytest.raises(AccountErasureBlockedError):
                erase_account_data(erasure_session, owner_row)
        finally:
            erasure_session.rollback()
            erasure_session.close()
    finally:
        t.join(timeout=5)

    assert claim_done.is_set(), "claim_next_job never completed -- possible deadlock"
    assert claim_result.get("value") is not None, "claim_next_job must have won the race and claimed the job"
    admin = _AdminSession()
    try:
        # Erasure correctly refused rather than racing to delete the account out from under
        # the worker that just claimed its job.
        assert admin.query(User).filter_by(id=owner_id).count() == 1
    finally:
        admin.close()


def test_erasure_winning_the_owner_lock_race_leaves_nothing_for_claim_next_job_to_claim(monkeypatch):
    """The other race ordering: erase_account_data wins the owner-lock race first for a
    PENDING job. claim_next_job must then find NOTHING claimable for this owner (both the
    job row and the owner are gone by the time it gets the lock) rather than claiming a job
    whose owner has just been erased -- the exact orphan-blob scenario the founder review
    described, now proven closed. Also proves the pending job's storage_key was correctly
    swept into the deletion inventory (no orphan left behind), and a second deadlock-timeout
    proof via the same bounded-join pattern as the test above."""
    import app.rag.account_erasure as account_erasure_module

    session = SessionLocal()
    try:
        owner = _make_user(session)
        owner_id = owner.id
        storage_key = _store_real_blob(b"erasure wins the owner-lock race")
        job = _make_import_job(session, owner.id, source_storage_key=storage_key, status=ImportJobStatus.pending)
        job_id = job.id
    finally:
        session.close()

    lock_acquired = threading.Event()
    proceed_to_commit = threading.Event()
    erasure_done = threading.Event()
    real_acquire = account_erasure_module.acquire_owner_erasure_lock

    def _pausing_acquire(db, owner_id_arg):
        real_acquire(db, owner_id_arg)
        lock_acquired.set()
        proceed_to_commit.wait(timeout=5)

    monkeypatch.setattr(account_erasure_module, "acquire_owner_erasure_lock", _pausing_acquire)

    erasure_result: dict = {}

    def _erasure_thread():
        erasure_session = SessionLocal()
        try:
            _set_rls_user(erasure_session, owner_id)
            owner_row = erasure_session.query(User).filter_by(id=owner_id).first()
            erasure_result["value"] = erase_account_data(erasure_session, owner_row)
        finally:
            erasure_done.set()
            erasure_session.close()

    t = threading.Thread(target=_erasure_thread)
    t.start()
    claim_db = _AdminSession()
    try:
        assert lock_acquired.wait(timeout=5), "erase_account_data never acquired the owner-erasure lock"
        proceed_to_commit.set()
        # Blocks on the SAME real Postgres advisory lock (a different connection) until
        # erasure's transaction above commits -- the exact serialization this test proves.
        claimed = claim_next_job(claim_db, "race-worker-erasure-wins", 120)
        t.join(timeout=5)

        assert erasure_done.is_set(), "erase_account_data never completed -- possible deadlock"
        assert claimed is None, "nothing must be claimable -- the owner and their pending job are both gone"
        assert claim_db.query(ImportJob).filter_by(id=job_id).count() == 0

        admin = _AdminSession()
        try:
            task_keys = {
                task.storage_key
                for task in admin.query(StorageDeletionTask).filter_by(operation_id=erasure_result["value"].operation_id).all()
            }
        finally:
            admin.close()
        assert storage_key in task_keys, (
            "the pending job's source_storage_key must have been swept into a deletion task -- "
            "otherwise it would be an orphaned blob with no owner left to ever reference it"
        )
    finally:
        claim_db.rollback()
        claim_db.close()


# --- storage_deletion_tasks: privilege boundary (Pass 28, founder review round 3) -----------
#
# The table-wide catalog checks (mainai_app has ZERO direct privileges) live in
# tests/backend/test_memory_source_units.py::test_mainai_app_privileges_are_exactly_least_
# privilege_no_truncate_references_trigger and ::test_apply_runtime_privileges_survives_a_
# second_boot (both extended in Pass 28 to cover storage_deletion_tasks' now-empty privilege
# set and enqueue_account_erasure_storage_task's EXECUTE grant). The tests below prove the
# SAME thing at RUNTIME, through this session's actual mainai_app connection -- not just the
# catalog's opinion of what it SHOULD be able to do.


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


def test_mainai_app_session_cannot_insert_directly_into_storage_deletion_tasks():
    """Pass 28: the one privilege Pass 27 still left mainai_app with (INSERT) is now revoked
    too -- see app/rag/account_erasure.py's module docstring for why plain INSERT access was
    itself dangerous (indirect access to a privileged physical-delete operation with no
    ownership check). The ONLY way an ordinary session may create a task row now is the
    enqueue_account_erasure_storage_task() SECURITY DEFINER function, exercised in the
    section below."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.add(
                StorageDeletionTask(
                    operation_id=uuid.uuid4(), storage_key=f"direct-insert-denied-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending
                )
            )
            session.commit()
        assert "permission denied" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


def test_public_lacks_execute_on_enqueue_account_erasure_storage_task():
    """No role should be able to call this function purely by connecting as PUBLIC -- only
    mainai_app, via the explicit grant in backend/scripts/s1a_privilege_policy.py, exactly
    like every other S1A SECURITY DEFINER function (migration 0019's module docstring)."""
    session = SessionLocal()
    try:
        has_exec = session.execute(
            sa_text(
                "SELECT has_function_privilege('public', "
                "'enqueue_account_erasure_storage_task(uuid, text)', 'EXECUTE')"
            )
        ).scalar()
        assert has_exec is False
    finally:
        session.rollback()
        session.close()


# --- enqueue_account_erasure_storage_task(): ownership-verified, idempotent enqueue (Pass 28)


def test_enqueue_account_erasure_storage_task_creates_a_row_for_a_caller_owned_document_key():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"owned document blob")
        _make_document(session, owner.id, storage_key=storage_key)
        operation_id = uuid.uuid4()

        _set_rls_user(session, owner.id)
        session.execute(
            sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
            {"operation_id": str(operation_id), "storage_key": storage_key},
        )
        session.commit()

        admin = _AdminSession()
        try:
            task = admin.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
            assert task.status == StorageDeletionStatus.pending
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_enqueue_account_erasure_storage_task_creates_a_row_for_a_caller_owned_import_job_key():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"owned import job blob")
        _make_import_job(session, owner.id, source_storage_key=storage_key)
        operation_id = uuid.uuid4()

        _set_rls_user(session, owner.id)
        session.execute(
            sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
            {"operation_id": str(operation_id), "storage_key": storage_key},
        )
        session.commit()

        admin = _AdminSession()
        try:
            assert admin.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).count() == 1
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_enqueue_account_erasure_storage_task_is_idempotent_on_operation_and_key():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        storage_key = _store_real_blob(b"idempotent enqueue proof")
        _make_document(session, owner.id, storage_key=storage_key)
        operation_id = uuid.uuid4()

        _set_rls_user(session, owner.id)
        for _ in range(3):
            session.execute(
                sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
                {"operation_id": str(operation_id), "storage_key": storage_key},
            )
        session.commit()

        admin = _AdminSession()
        try:
            assert admin.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).count() == 1
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_enqueue_account_erasure_storage_task_denies_a_key_owned_by_a_different_owner():
    """The exact cross-owner gap the founder review named: nothing may queue a physical
    delete for a storage_key it doesn't actually own, no matter how it was found."""
    session = SessionLocal()
    try:
        victim = _make_user(session, email=f"victim-{uuid.uuid4().hex[:8]}@example.com")
        attacker = _make_user(session, email=f"attacker-{uuid.uuid4().hex[:8]}@example.com")
        storage_key = _store_real_blob(b"belongs to the victim only")
        _make_document(session, victim.id, storage_key=storage_key)

        _set_rls_user(session, attacker.id)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(
                sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
                {"operation_id": str(uuid.uuid4()), "storage_key": storage_key},
            )
            session.commit()
        assert "not owned by the caller" in str(exc_info.value).lower()

        admin = _AdminSession()
        try:
            assert admin.query(StorageDeletionTask).filter_by(storage_key=storage_key).count() == 0
        finally:
            admin.close()
    finally:
        session.rollback()
        session.close()


def test_enqueue_account_erasure_storage_task_denies_an_arbitrary_unreferenced_key():
    """A storage_key that references nothing real at all -- neither a Document nor an
    ImportJob -- must be refused, not silently accepted."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(
                sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
                {"operation_id": str(uuid.uuid4()), "storage_key": "sha256:nonexistent-arbitrary-key"},
            )
            session.commit()
        assert "not owned by the caller" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


def test_enqueue_account_erasure_storage_task_denies_a_project_memory_storage_key():
    """app/project_memory.py's ProjectSource/ProjectCheckpoint blobs are founder-wide project
    state, never a per-user Document/ImportJob (see account_erasure.py's blob-write-path
    audit) -- their storage_keys must be just as unenqueueable as any other unowned key, even
    though a real file exists on disk for them."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        project_memory_key = _store_real_blob(b"founder-wide project memory blob, not user-owned")

        _set_rls_user(session, owner.id)
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(
                sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
                {"operation_id": str(uuid.uuid4()), "storage_key": project_memory_key},
            )
            session.commit()
        assert "not owned by the caller" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


def test_enqueue_account_erasure_storage_task_requires_an_authenticated_caller():
    session = SessionLocal()
    try:
        session.execute(sa_text("SET LOCAL app.current_user_id = ''"))
        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(
                sa_text("SELECT enqueue_account_erasure_storage_task(:operation_id, :storage_key)"),
                {"operation_id": str(uuid.uuid4()), "storage_key": "sha256:whatever"},
            )
            session.commit()
        assert "no authenticated user context" in str(exc_info.value).lower()
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


def test_claim_storage_deletion_tasks_include_failed_false_never_claims_a_failed_task():
    """Pass 28: the exact mechanism the immediate-attempt infinite-retry-loop fix depends on
    -- with include_failed=False, a `failed` task is invisible to this claim no matter its
    next_attempt_at, only `pending`/lease-expired `processing` rows are eligible."""
    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        t_pending = StorageDeletionTask(operation_id=op_id, storage_key=f"incl-failed-pending-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending)
        t_failed_ready = StorageDeletionTask(
            operation_id=op_id, storage_key=f"incl-failed-ready-{uuid.uuid4().hex}", status=StorageDeletionStatus.failed
        )
        admin.add_all([t_pending, t_failed_ready])
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10, operation_id=op_id, include_failed=False)

        assert claimed == [t_pending.id]
        admin.expire_all()
        assert admin.get(StorageDeletionTask, t_failed_ready.id).status == StorageDeletionStatus.failed  # untouched
    finally:
        admin.rollback()
        admin.close()


def test_claim_storage_deletion_tasks_respects_a_failed_tasks_next_attempt_at_backoff():
    """Even with the default include_failed=True (the worker's own retry scan), a `failed`
    task whose backoff hasn't elapsed yet must not be reclaimed -- only once `next_attempt_at`
    is in the past does it become eligible again."""
    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        not_yet = StorageDeletionTask(
            operation_id=op_id,
            storage_key=f"backoff-not-yet-{uuid.uuid4().hex}",
            status=StorageDeletionStatus.failed,
            next_attempt_at=datetime.utcnow() + timedelta(hours=1),
        )
        elapsed = StorageDeletionTask(
            operation_id=op_id,
            storage_key=f"backoff-elapsed-{uuid.uuid4().hex}",
            status=StorageDeletionStatus.failed,
            next_attempt_at=datetime.utcnow() - timedelta(seconds=1),
        )
        admin.add_all([not_yet, elapsed])
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10, operation_id=op_id)

        assert elapsed.id in claimed
        assert not_yet.id not in claimed
    finally:
        admin.rollback()
        admin.close()


def test_claim_storage_deletion_tasks_never_reclaims_a_terminal_purged_or_retained_shared_task():
    """Pass 28 point 4 (claim state-transition verification): a terminal-success task must
    never become claimable again, even if its updated_at looks arbitrarily old -- the WHERE
    clause's lease-expiry branch only ever applies to status='processing', never to a
    genuinely finished task."""
    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        purged = StorageDeletionTask(operation_id=op_id, storage_key=f"terminal-purged-{uuid.uuid4().hex}", status=StorageDeletionStatus.purged)
        retained = StorageDeletionTask(
            operation_id=op_id, storage_key=f"terminal-retained-{uuid.uuid4().hex}", status=StorageDeletionStatus.retained_shared
        )
        admin.add_all([purged, retained])
        admin.commit()
        admin.execute(
            sa_text("UPDATE storage_deletion_tasks SET updated_at = now() - interval '1 year' WHERE operation_id = :op"),
            {"op": str(op_id)},
        )
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10, operation_id=op_id, lease_seconds=1)

        assert claimed == []
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


# --- attempt_pending_storage_deletions_for_operation(): immediate-attempt infinite-retry-loop
# fix (Pass 28, founder review round 3) -------------------------------------------------------


def test_attempt_pending_storage_deletions_for_operation_tries_a_permanently_failing_task_exactly_once(monkeypatch):
    """The concrete bug the founder review named: with the OLD claim_storage_deletion_tasks
    default (include_failed=True), a task that fails with a PERMANENT StorageError got
    reclaimed by the very next iteration of this function's own `while True` loop --
    forever, an unbounded busy loop inside a single HTTP request. Proven here by counting
    real storage.delete() calls: a permanently-broken storage backend must be tried exactly
    once per task, never looped on."""
    import app.rag.account_erasure as account_erasure_module

    delete_calls: list[str] = []

    class _AlwaysBrokenStorage:
        def delete(self, key):
            delete_calls.append(key)
            raise StorageError("simulated PERMANENT I/O failure -- retrying can never fix this")

        def exists(self, key):
            return False

    monkeypatch.setattr(account_erasure_module, "get_storage", lambda: _AlwaysBrokenStorage())

    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        task_keys = [f"permanent-fail-{i}-{uuid.uuid4().hex}" for i in range(3)]
        for key in task_keys:
            admin.add(StorageDeletionTask(operation_id=op_id, storage_key=key, status=StorageDeletionStatus.pending))
        admin.commit()

        result = attempt_pending_storage_deletions_for_operation(op_id)

        assert sorted(delete_calls) == sorted(task_keys), "each task must be attempted exactly once, not looped on"
        assert set(result.storage_tasks_pending_or_failed) == {
            str(t.id) for t in admin.query(StorageDeletionTask).filter_by(operation_id=op_id).all()
        }
        admin.expire_all()
        for t in admin.query(StorageDeletionTask).filter_by(operation_id=op_id).all():
            assert t.status == StorageDeletionStatus.failed
            assert t.attempt_count == 1
            assert t.next_attempt_at is not None  # left for the worker's own backed-off retry
    finally:
        admin.rollback()
        admin.close()


def test_attempt_pending_storage_deletions_for_operation_never_reclaims_a_task_that_just_failed(monkeypatch):
    """Direct proof of the fix's actual mechanism: claim_storage_deletion_tasks is called
    with include_failed=False, so a task that transitions pending -> failed mid-call can
    never be seen again by the SAME call, regardless of how many tasks exist."""
    import app.rag.account_erasure as account_erasure_module

    real_claim = account_erasure_module.claim_storage_deletion_tasks
    seen_include_failed: list[bool] = []

    def _spy_claim(db, **kwargs):
        seen_include_failed.append(kwargs.get("include_failed", True))
        return real_claim(db, **kwargs)

    monkeypatch.setattr(account_erasure_module, "claim_storage_deletion_tasks", _spy_claim)

    class _AlwaysBrokenStorage:
        def delete(self, key):
            raise StorageError("simulated permanent failure")

        def exists(self, key):
            return False

    monkeypatch.setattr(account_erasure_module, "get_storage", lambda: _AlwaysBrokenStorage())

    admin = _AdminSession()
    try:
        op_id = uuid.uuid4()
        admin.add(StorageDeletionTask(operation_id=op_id, storage_key=f"spy-proof-{uuid.uuid4().hex}", status=StorageDeletionStatus.pending))
        admin.commit()

        attempt_pending_storage_deletions_for_operation(op_id)

        assert seen_include_failed, "claim_storage_deletion_tasks was never called"
        assert all(v is False for v in seen_include_failed), "every claim from the immediate attempt must pass include_failed=False"
    finally:
        admin.rollback()
        admin.close()


def test_worker_retry_can_still_reclaim_a_task_the_immediate_attempt_left_failed(monkeypatch):
    """The other half of the fix: leaving a permanently-failed task to the worker must not
    mean it's stuck forever -- once its backoff elapses, the worker's own scan
    (include_failed=True, the default) picks it back up, same as any other failed task."""
    admin = _AdminSession()
    try:
        task = StorageDeletionTask(
            operation_id=uuid.uuid4(),
            storage_key=f"worker-reclaims-{uuid.uuid4().hex}",
            status=StorageDeletionStatus.failed,
            attempt_count=1,
            next_attempt_at=datetime.utcnow() - timedelta(seconds=1),
        )
        admin.add(task)
        admin.commit()

        claimed = claim_storage_deletion_tasks(admin, limit=10)  # worker's own default scan

        assert task.id in claimed
    finally:
        admin.rollback()
        admin.close()


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
    a native Postgres enum type that was never actually created.

    Pass 28: runs on `_AdminSession`, not the ordinary mainai_app-bound `SessionLocal` --
    `information_schema.columns` itself filters out columns the querying role has no
    privilege on at all, and mainai_app now has ZERO direct privileges on this table (see
    module docstring)."""
    session = _AdminSession()
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
