"""Privilege coverage for the Life Source Foundation Bootstrap (migration 0037,
docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §D/§L) — two separate mechanisms in
backend/scripts/security/s1a_privilege_policy.py:

1. Table-level narrowing (`_PROTECTED_TABLES`) for the three new S1C/corpus-manifest tables —
   same mechanism tests/backend/test_ensure_app_role.py already covers for the pre-existing
   S1A tables, applied here to `message_source_units` / `source_import_batches` /
   `source_import_batch_failures`.

2. Column-level narrowing (`_COLUMN_PROTECTED_TABLES`) — the P0 original-source-immutability
   invariant the founder's bootstrap mandate named explicitly: `documents.storage_key` and
   `documents.file_path` (the Source Vault original a Document points at) must never be
   UPDATE-able by `mainai_app`, while every OTHER column on `documents` stays fully writable.
   This is the mechanism a real empirical bug was found and fixed in while building this pass
   (see the module's own comment on Postgres's has_column_privilege semantics) — these tests
   are the regression proof for that fix, run against a real Postgres instance, not asserted
   from the text of a GRANT statement.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.user import User, UserRole
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"
_POLICY_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "security" / "s1a_privilege_policy.py"


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


@pytest.fixture
def restore_privileges():
    """Mirrors tests/backend/test_runtime_table_privileges.py's fixture of the same name —
    restores the uniform DML floor after a test deliberately widens mainai_app's privileges,
    so later tests/modules aren't left running against a mutated privilege environment."""
    yield
    settings = get_settings()
    import psycopg2

    admin = psycopg2.connect(settings.database_url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app")
    admin.close()
    module = _load(_APPLY_RUNTIME_PRIVILEGES_PATH, "apply_runtime_privileges")
    module.apply_and_verify(settings.database_url)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="privilege-owner@example.com") -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_document(session, owner_id, *, title="Kalla") -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
        storage_key="originals/abc123",
        file_path="/legacy/path.pdf",
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


# --- column-level immutability: documents.storage_key / documents.file_path -----------------


def test_mainai_app_cannot_update_storage_key_or_file_path_columns():
    with migration_engine.connect() as conn:
        for column in ("storage_key", "file_path"):
            held = conn.execute(
                sa_text("SELECT has_column_privilege('mainai_app', 'public.documents', :c, 'UPDATE')"),
                {"c": column},
            ).scalar()
            assert held is False, f"mainai_app must not be able to UPDATE documents.{column} -- P0 Source Vault immutability"


def test_mainai_app_has_no_table_level_update_on_documents():
    """The regression proof for the exact bug this pass found and fixed: a surviving TABLE-level
    UPDATE grant would make has_column_privilege() return true for storage_key/file_path too,
    silently defeating the column-level REVOKE above. This must be gone entirely -- UPDATE is
    only ever granted at column level now."""
    with migration_engine.connect() as conn:
        held = conn.execute(
            sa_text("SELECT has_table_privilege('mainai_app', 'public.documents', 'UPDATE')")
        ).scalar()
        assert held is False


def test_mainai_app_can_still_update_other_document_columns():
    """The narrowing must not have overshot -- every OTHER column on documents (e.g. `title`,
    `active_truth_status`) must remain fully writable; only storage_key/file_path are
    protected."""
    with migration_engine.connect() as conn:
        for column in ("title", "active_truth_status"):
            held = conn.execute(
                sa_text("SELECT has_column_privilege('mainai_app', 'public.documents', :c, 'UPDATE')"),
                {"c": column},
            ).scalar()
            assert held is True, f"mainai_app must still be able to UPDATE documents.{column}"


def test_real_update_of_storage_key_is_rejected_end_to_end(restore_privileges):
    """Not just a catalog-privilege check -- an ACTUAL UPDATE statement issued through the
    restricted runtime role (app.db.SessionLocal, exactly what application code uses) against
    storage_key must fail with an insufficient-privilege error, while an UPDATE of an
    unprotected column on the same row succeeds."""
    import psycopg2.errors
    from sqlalchemy.exc import DBAPIError

    session = SessionLocal()
    try:
        owner = _make_user(session, "end-to-end-immutable@example.com")
        document = _make_document(session, owner.id)
        _set_rls_user(session, owner.id)

        with pytest.raises(DBAPIError) as excinfo:
            session.execute(
                sa_text("UPDATE documents SET storage_key = :new WHERE id = :id"),
                {"new": "tampered/key", "id": str(document.id)},
            )
            session.commit()
        session.rollback()
        assert isinstance(excinfo.value.orig, psycopg2.errors.InsufficientPrivilege)

        _set_rls_user(session, owner.id)
        session.execute(sa_text("UPDATE documents SET title = :t WHERE id = :id"), {"t": "Nytt namn", "id": str(document.id)})
        session.commit()
        session.refresh(document)
        assert document.title == "Nytt namn"
        assert document.storage_key == "originals/abc123"  # untouched by the rejected attempt
    finally:
        session.rollback()
        session.close()


def test_column_protected_tables_covers_documents_storage_key_and_file_path():
    """Sanity check at the policy-source level, mirroring
    tests/backend/rag/test_memory_source_backfill_run.py's test_policy_registry_backfill_
    tables_present -- the policy module itself must still declare this protection, not just
    happen to produce the right DB state on this run."""
    policy = _load(_POLICY_PATH, "s1a_privilege_policy")
    protected = {(table, priv): set(cols) for table, priv, cols in policy._COLUMN_PROTECTED_TABLES}
    assert ("documents", "UPDATE") in protected
    assert protected[("documents", "UPDATE")] == {"storage_key", "file_path"}


def test_verify_only_mode_agrees_column_protection_is_in_place():
    """The durable-worker container's boot path (--verify-only, apply_privilege_policy(...,
    mutate=False)) must independently confirm the column-level state too, not just the
    table-level `_PROTECTED_TABLES` checks it already covered before this bootstrap pass."""
    policy = _load(_POLICY_PATH, "s1a_privilege_policy")
    settings = get_settings()
    import psycopg2

    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = policy.apply_privilege_policy(cur, expected_owner=expected_owner, require_complete=True, mutate=False)
        assert errors == [], f"read-only verification found policy mismatches: {errors}"
    finally:
        conn.rollback()
        conn.close()
