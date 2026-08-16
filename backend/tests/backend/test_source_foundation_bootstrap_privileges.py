"""Privilege/immutability coverage for the Life Source Foundation Bootstrap (migration 0037,
docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §D/§L) — two separate mechanisms:

1. Table-level narrowing (`_PROTECTED_TABLES` in backend/scripts/security/s1a_privilege_policy.py)
   for the three new S1C/corpus-manifest tables — same mechanism
   tests/backend/test_ensure_app_role.py already covers for the pre-existing S1A tables, applied
   here to `message_source_units` / `source_import_batches` / `source_import_batch_failures`.

2. `documents.storage_key` / `documents.file_path` write-once immutability (the P0 Source Vault
   invariant the founder's bootstrap mandate named explicitly) — enforced by migration 0037's
   `trg_documents_storage_immutable` BEFORE UPDATE trigger, NOT by privilege narrowing. An
   earlier version of this pass tried column-level privilege narrowing here and it broke a real
   production code path (app/rag/library_import.py's legitimate NULL -> value first write via
   UPDATE, once a blob is durably stored) — found empirically by running the full test suite,
   not assumed correct from a design read. The trigger is the correct primitive: it allows the
   one-time NULL -> value transition every document goes through, and rejects only a LATER
   change to an already-set value, which privilege GRANT/REVOKE (binary, not value-conditional)
   cannot express. These tests are the regression proof for both the original invariant and the
   fix, run against a real Postgres instance.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.user import User, UserRole
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """Same reasoning as tests/backend/rag/test_memory_source_units.py's identical fixture: the
    migration itself does not REVOKE anything (a fresh `alembic upgrade head` on a database
    where mainai_app doesn't exist yet would fail outright) — narrowing is
    apply_runtime_privileges.py's job, applied here explicitly so this module's assertions
    don't depend on test execution order."""
    module = _load(_APPLY_RUNTIME_PRIVILEGES_PATH, "apply_runtime_privileges")
    module.apply_and_verify(get_settings().database_url)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="privilege-owner@example.com") -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_document(session, owner_id, *, title="Kalla", storage_key="originals/abc123", file_path="/legacy/path.pdf") -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
        storage_key=storage_key,
        file_path=file_path,
    )
    session.add(document)
    session.commit()
    return document


# --- table-level narrowing for the new S1C/corpus-manifest tables ---------------------------


def test_mainai_app_has_no_direct_privileges_on_message_source_units():
    with migration_engine.connect() as conn:
        for priv in ("UPDATE", "DELETE"):
            held = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'public.message_source_units', :p)"),
                {"p": priv},
            ).scalar()
            assert held is False, f"mainai_app must not hold {priv} on message_source_units directly"
        for priv in ("SELECT", "INSERT"):
            held = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'public.message_source_units', :p)"),
                {"p": priv},
            ).scalar()
            assert held is True, f"mainai_app must hold {priv} on message_source_units"


def test_mainai_app_has_select_insert_update_but_not_delete_on_source_import_batches():
    with migration_engine.connect() as conn:
        for priv in ("SELECT", "INSERT", "UPDATE"):
            held = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'public.source_import_batches', :p)"),
                {"p": priv},
            ).scalar()
            assert held is True, f"mainai_app must hold {priv} on source_import_batches (progress counters advance)"
        held = conn.execute(
            sa_text("SELECT has_table_privilege('mainai_app', 'public.source_import_batches', 'DELETE')")
        ).scalar()
        assert held is False, "a batch's history must be kept -- deleting it would make its completeness proof unreconstructable"


def test_mainai_app_has_no_update_or_delete_on_source_import_batch_failures():
    with migration_engine.connect() as conn:
        for priv in ("SELECT", "INSERT"):
            held = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'public.source_import_batch_failures', :p)"),
                {"p": priv},
            ).scalar()
            assert held is True
        for priv in ("UPDATE", "DELETE"):
            held = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'public.source_import_batch_failures', :p)"),
                {"p": priv},
            ).scalar()
            assert held is False, f"parser-failure records must be append-only: mainai_app must not hold {priv}"


def test_mainai_app_keeps_full_table_level_update_on_documents():
    """The regression proof for the exact conflict this pass found and resolved: documents must
    keep ordinary table-level UPDATE (every other test exercising the upload/processing pipeline
    depends on it), because storage_key/file_path immutability is now enforced by a trigger, not
    by narrowing this grant."""
    with migration_engine.connect() as conn:
        held = conn.execute(
            sa_text("SELECT has_table_privilege('mainai_app', 'public.documents', 'UPDATE')")
        ).scalar()
        assert held is True


# --- documents.storage_key / documents.file_path write-once immutability (trigger) ----------


def test_first_write_of_storage_key_from_null_succeeds():
    """The legitimate case this invariant must NOT block: app/rag/library_import.py creates the
    Document row with storage_key/file_path still NULL, then sets them once the blob is durably
    stored — a real UPDATE, not an INSERT."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "first-write@example.com")
        document = _make_document(session, owner.id, storage_key=None, file_path=None)
        _set_rls_user(session, owner.id)

        document.storage_key = "originals/first-write-key"
        document.file_path = "/vault/first-write-key"
        session.commit()
        session.refresh(document)
        assert document.storage_key == "originals/first-write-key"
        assert document.file_path == "/vault/first-write-key"
    finally:
        session.rollback()
        session.close()


def test_changing_an_already_set_storage_key_is_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session, "change-rejected@example.com")
        document = _make_document(session, owner.id, storage_key="originals/original-key")
        _set_rls_user(session, owner.id)

        document.storage_key = "originals/a-different-key"
        with pytest.raises((IntegrityError, DBAPIError), match="storage_key is immutable"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_changing_an_already_set_file_path_is_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session, "change-file-path-rejected@example.com")
        document = _make_document(session, owner.id, file_path="/vault/original.pdf")
        _set_rls_user(session, owner.id)

        document.file_path = "/vault/tampered.pdf"
        with pytest.raises((IntegrityError, DBAPIError), match="file_path is immutable"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_setting_storage_key_to_the_same_value_is_a_harmless_no_op():
    """Re-asserting the SAME value (OLD == NEW) must not be treated as a change -- some code
    paths may legitimately re-set an attribute that happens not to have changed."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "no-op-same-value@example.com")
        document = _make_document(session, owner.id, storage_key="originals/stable-key")
        _set_rls_user(session, owner.id)

        session.execute(
            sa_text("UPDATE documents SET storage_key = :same WHERE id = :id"),
            {"same": "originals/stable-key", "id": str(document.id)},
        )
        session.commit()
        session.refresh(document)
        assert document.storage_key == "originals/stable-key"
    finally:
        session.rollback()
        session.close()


def test_updating_other_document_columns_is_unaffected_by_the_guard():
    """The trigger must only ever fire on storage_key/file_path changes -- every other column
    (title, active_truth_status, ...) stays freely writable, in the same UPDATE or a separate
    one."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "other-columns-unaffected@example.com")
        document = _make_document(session, owner.id)
        _set_rls_user(session, owner.id)

        document.title = "Nytt namn"
        document.active_truth_status = ActiveTruthStatus.superseded
        session.commit()
        session.refresh(document)
        assert document.title == "Nytt namn"
        assert document.active_truth_status == ActiveTruthStatus.superseded
        assert document.storage_key == "originals/abc123"  # untouched
    finally:
        session.rollback()
        session.close()


def test_real_two_phase_upload_flow_survives_the_guard_end_to_end():
    """Mirrors app/rag/library_import.py's real sequence: INSERT with storage_key NULL, then a
    single later UPDATE setting storage_key/file_path/status/size_bytes together once the blob
    write succeeds -- through the restricted runtime role, exactly what production does."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "two-phase-upload@example.com")
        document = _make_document(session, owner.id, storage_key=None, file_path=None)
        _set_rls_user(session, owner.id)
        assert document.storage_key is None

        document.storage_key = "originals/two-phase-key"
        document.size_bytes = 4096
        document.title = "Uppdaterad titel"
        session.commit()
        session.refresh(document)
        assert document.storage_key == "originals/two-phase-key"
        assert document.size_bytes == 4096
        assert document.title == "Uppdaterad titel"

        # A SECOND attempt to change the now-set storage_key must still be rejected.
        document.storage_key = "originals/a-hijacked-key"
        with pytest.raises((IntegrityError, DBAPIError), match="storage_key is immutable"):
            session.commit()
    finally:
        session.rollback()
        session.close()
