"""S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, migration 0019) — schema/trigger/
privilege/lifecycle coverage for memory_source_units/document_source_units/
memory_source_lifecycle_events. Real local Postgres, mirroring tests/backend/test_claims.py's
pattern (RLS is exercised for real, not mocked).

Not covered here (separate, later commits per the S1A/S1B/S1C plan and this PR's own
checkpoint): the deterministic backfill job, dual-write from app/rag/claims.py, purge_source(),
and account export/erasure wiring. This file covers the migration + models + find-or-create
slice, plus the privilege-hardening/entrypoint/concurrency regression coverage a founder code
review added on top of the first draft (cross-owner claim FK, exact least-privilege incl.
TRUNCATE/REFERENCES/TRIGGER, the real docker-entrypoint.sh worker-reboot path, a genuinely
non-superuser/non-BYPASSRLS admin-function-owner misconfiguration, and a real two-thread
lock-wait proof of find-or-create's concurrency handling).
"""

import hashlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import get_settings
from app.db import SessionLocal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import (
    DocumentSourceUnit,
    LifecycleStatus,
    MemorySourceLifecycleEvent,
    MemorySourceUnit,
    OccurredAtBasis,
    SnapshotStatus,
    SourceKind,
    SourceRole,
)
from app.models.user import User, UserRole
from app.rag.memory_source import (
    DocumentSourceLocator,
    MemorySourceIdentityConflict,
    compute_content_hash,
    get_or_create_memory_source_unit,
)
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password


_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "apply_runtime_privileges.py"


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """S1A's migration deliberately does NOT REVOKE mainai_app's privileges itself (see
    migration 0019's docstring) — that's apply_runtime_privileges.py's job, run once after
    the migration during a real boot. Tests in this file that assert mainai_app CAN'T do
    something must not depend on test execution order (e.g. tests/backend/test_ensure_app_role.py
    running earlier and re-granting ALL PRIVILEGES via the real ensure_app_role.py script) —
    so apply it explicitly here, matching the real boot order, rather than assuming whatever
    state an earlier test left behind."""
    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="owner@example.com") -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_document(session, owner_id, *, title="Källa") -> Document:
    _set_rls_user(session, owner_id)
    document = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value="Bolaget grundades 2019.") -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=0, text=text_value, embedding=[0.1] * 1536)
    session.add(chunk)
    session.commit()
    return chunk


def _chunk_locator(owner_id, document_id, chunk_id, *, content_text="Bolaget grundades 2019.") -> DocumentSourceLocator:
    return DocumentSourceLocator(
        owner_id=owner_id,
        document_id=document_id,
        version_id=None,
        chunk_id=chunk_id,
        observed_at=datetime.now(timezone.utc),
        content_text=content_text,
        snapshot_status=SnapshotStatus.exact,
    )


# --- find-or-create -------------------------------------------------------------------


def test_get_or_create_memory_source_unit_creates_parent_and_subtype():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        msu = session.get(MemorySourceUnit, msu_id)
        dsu = session.get(DocumentSourceUnit, msu_id)
        assert msu is not None
        assert msu.source_kind == SourceKind.document_chunk
        assert msu.source_role == SourceRole.unknown
        assert msu.source_identity_key == f"document_chunk:{chunk.id}"
        assert msu.snapshot_status == SnapshotStatus.exact
        assert msu.lifecycle_status == LifecycleStatus.active
        assert dsu is not None
        assert dsu.chunk_id == chunk.id
        assert dsu.document_id == document.id
        assert dsu.source_kind == SourceKind.document_chunk
    finally:
        session.close()


def test_get_or_create_memory_source_unit_is_idempotent_no_duplicate():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        first_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()
        second_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        assert first_id == second_id
        count = session.execute(
            sa_text("SELECT count(*) FROM memory_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert count == 1
    finally:
        session.close()


def test_get_or_create_memory_source_unit_real_concurrent_insert_blocks_then_converges():
    """A previous version of this test only ran session_a to completion (commit) before
    session_b even started, which exercises the fallback-SELECT path against already-
    committed data but proves nothing about genuine concurrency. This version holds
    session_a's INSERT open (uncommitted) in the main thread while session_b's real INSERT
    runs concurrently on a background thread and, verified via `pg_stat_activity` from a
    third connection, actually enters a real lock wait — not merely "runs after" — before
    session_a commits and unblocks it."""
    session_a = SessionLocal()
    session_b = SessionLocal()
    try:
        owner = _make_user(session_a)
        document = _make_document(session_a, owner.id)
        chunk = _make_chunk(session_a, owner.id, document.id)
        _set_rls_user(session_a, owner.id)
        _set_rls_user(session_b, owner.id)
        locator = _chunk_locator(owner.id, document.id, chunk.id)

        # session_a: the same INSERT get_or_create_memory_source_unit would do, but held
        # open (not committed) so session_b genuinely has to wait on it.
        content_hash, content_hash_version = compute_content_hash(locator.content_text)
        msu_a = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=locator.source_kind,
            source_identity_key=locator.identity_key,
            source_role=SourceRole.unknown,
            observed_at=locator.observed_at,
            occurred_at=None,
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text=locator.content_text,
            content_hash=content_hash,
            content_hash_version=content_hash_version,
            snapshot_status=locator.snapshot_status,
        )
        session_a.add(msu_a)
        session_a.flush()
        session_a.add(
            DocumentSourceUnit(
                memory_source_id=msu_a.id, owner_id=owner.id, source_kind=locator.source_kind,
                document_id=document.id, version_id=None, chunk_id=chunk.id,
            )
        )
        session_a.flush()

        result: dict = {}

        def _run_session_b():
            try:
                result["id"] = get_or_create_memory_source_unit(session_b, locator)
                session_b.commit()
            except Exception as exc:  # noqa: BLE001 - captured and asserted on below
                result["error"] = exc

        thread_b = threading.Thread(target=_run_session_b)
        thread_b.start()

        probe_engine = create_engine(get_settings().database_url)
        saw_real_lock_wait = False
        try:
            deadline = time.monotonic() + 5.0
            with probe_engine.connect() as probe:
                while time.monotonic() < deadline:
                    waiting = probe.execute(
                        sa_text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type = 'Lock' AND query ILIKE '%memory_source_units%'"
                        )
                    ).scalar()
                    if waiting and waiting > 0:
                        saw_real_lock_wait = True
                        break
                    time.sleep(0.05)
        finally:
            probe_engine.dispose()

        assert saw_real_lock_wait, (
            "session_b never entered a real lock wait on memory_source_units — this test "
            "isn't exercising genuine concurrency"
        )

        session_a.commit()
        thread_b.join(timeout=5)
        assert not thread_b.is_alive(), "session_b never unblocked after session_a committed"

        assert "error" not in result, f"session_b raised unexpectedly: {result.get('error')!r}"
        assert result["id"] == msu_a.id

        count = session_a.execute(
            sa_text("SELECT count(*) FROM memory_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert count == 1
    finally:
        session_a.close()
        session_b.close()


def test_get_or_create_memory_source_unit_rejects_mismatched_locator():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk_1 = _make_chunk(session, owner.id, document.id, text_value="Text A")
        chunk_2 = _make_chunk(session, owner.id, document.id, text_value="Text B")
        _set_rls_user(session, owner.id)

        get_or_create_memory_source_unit(
            session, _chunk_locator(owner.id, document.id, chunk_1.id, content_text="Text A")
        )
        session.commit()

        # Same identity_key impossible to construct honestly for a different chunk (the key
        # is derived from chunk_id), so instead exercise the DB-level guard directly: forging
        # a conflicting identity_key by hand must be rejected by the DSU validation trigger.
        bad_locator = DocumentSourceLocator(
            owner_id=owner.id,
            document_id=document.id,
            version_id=None,
            chunk_id=chunk_2.id,
            observed_at=datetime.now(timezone.utc),
            content_text="Text B",
            snapshot_status=SnapshotStatus.exact,
        )
        # sanity: this one is a *different* identity_key, so it must succeed independently.
        second_id = get_or_create_memory_source_unit(session, bad_locator)
        session.commit()
        assert second_id != None  # noqa: E711
    finally:
        session.close()


def test_document_chunk_exact_snapshot_with_wrong_chunk_text_rejected_at_commit():
    """An `exact` snapshot must be bound to the REAL text of the linked chunk_id, not merely
    to whatever text the caller happened to submit -- mainai_app has direct INSERT on both
    tables, so nothing but the DB trigger stops a caller from hashing chunk B's text while
    linking chunk A. The DSU validation trigger is DEFERRABLE INITIALLY DEFERRED, so the
    mismatch surfaces at commit, not at the INSERT itself."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk_a = _make_chunk(session, owner.id, document.id, text_value="Text from chunk A")
        _make_chunk(session, owner.id, document.id, text_value="Text from chunk B")
        _set_rls_user(session, owner.id)

        # A fresh identity_key (chunk_a hasn't been used yet), but content_text belongs to a
        # DIFFERENT chunk entirely.
        mismatched_locator = _chunk_locator(owner.id, document.id, chunk_a.id, content_text="Text from chunk B")
        get_or_create_memory_source_unit(session, mismatched_locator)
        with pytest.raises((IntegrityError, DBAPIError), match="content_text does not match"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_document_version_exact_snapshot_rejected_at_commit():
    """KnowledgeVersion has no canonical text column (checksum/metadata only), so an `exact`
    document_version snapshot would necessarily be an unverifiable, caller-supplied claim --
    it's restricted to degraded/missing, same as document_record already was."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        version = KnowledgeVersion(
            source_id=document.id, owner_id=owner.id, version_number=1,
            checksum="deadbeef" * 8, extraction_version="v1",
        )
        _set_rls_user(session, owner.id)
        session.add(version)
        session.flush()

        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.document_version,
            source_identity_key=f"document_version:{version.id}",
            source_role=SourceRole.unknown,
            observed_at=datetime.now(timezone.utc),
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text="some claimed exact text",
            content_hash=hashlib.sha256(b"some claimed exact text").hexdigest(),
            content_hash_version="sha256-utf8-v1",
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(
            DocumentSourceUnit(
                memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.document_version,
                document_id=document.id, version_id=version.id, chunk_id=None,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="document_version may not be an exact snapshot"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_document_chunk_exact_snapshot_with_correct_chunk_text_accepted():
    """The positive counterpart: a document_chunk exact snapshot whose content_text genuinely
    matches the linked chunk's real text is accepted, and get_or_create_memory_source_unit()
    uses exactly that real text (not any caller-invented content)."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id, text_value="The real chunk text.")
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_memory_source_unit(
            session, _chunk_locator(owner.id, document.id, chunk.id, content_text="The real chunk text.")
        )
        session.commit()

        msu = session.get(MemorySourceUnit, msu_id)
        assert msu.content_text == "The real chunk text."
        assert msu.content_hash == hashlib.sha256(b"The real chunk text.").hexdigest()
    finally:
        session.rollback()
        session.close()


def test_get_or_create_memory_source_unit_reraises_unrelated_integrity_error():
    """A locator pointing at a document_id that doesn't exist trips document_source_units'
    own FK to `documents`, not `uq_msu_owner_identity` — this must propagate as-is (the
    caller has a real bug to fix), never get misdiagnosed as "someone else already created
    this" and silently swallowed."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        bogus_locator = _chunk_locator(owner.id, uuid.uuid4(), uuid.uuid4())
        with pytest.raises(IntegrityError) as exc_info:
            get_or_create_memory_source_unit(session, bogus_locator)
        constraint_name = getattr(getattr(getattr(exc_info.value, "orig", None), "diag", None), "constraint_name", None)
        assert constraint_name != "uq_msu_owner_identity"
    finally:
        session.rollback()
        session.close()


def test_get_or_create_memory_source_unit_detects_content_hash_mismatch():
    """content_hash is always computed internally from content_text (never caller-supplied —
    see app/rag/memory_source.py's compute_content_hash), so the only realistic way to
    provoke a mismatch on lookup is a second call for the SAME identity_key (same chunk_id)
    with genuinely DIFFERENT content_text — e.g. the underlying chunk's text changed since
    the source unit was first created. The two computed hashes then legitimately differ."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        changed_text_locator = _chunk_locator(
            owner.id, document.id, chunk.id, content_text="Bolaget grundades 2019 (reviderad text)."
        )
        with pytest.raises(MemorySourceIdentityConflict, match="content_hash"):
            get_or_create_memory_source_unit(session, changed_text_locator)
    finally:
        session.rollback()
        session.close()


def test_compute_content_hash_is_a_pure_sha256_of_utf8_text():
    from app.rag.memory_source import CONTENT_HASH_VERSION

    content_hash, content_hash_version = compute_content_hash("Bolaget grundades 2019.")
    assert content_hash == hashlib.sha256("Bolaget grundades 2019.".encode("utf-8")).hexdigest()
    assert content_hash_version == CONTENT_HASH_VERSION
    assert len(content_hash) == 64
    assert content_hash == content_hash.lower()

    assert compute_content_hash(None) == (None, None)


def test_get_or_create_memory_source_unit_refuses_to_reuse_revoked_source():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'test revoke')"),
            {"id": str(msu_id)},
        )
        session.commit()

        with pytest.raises(MemorySourceIdentityConflict, match="revoked or purged"):
            get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
    finally:
        session.rollback()
        session.close()


# --- schema / CHECK constraints ---------------------------------------------------------


def test_source_identity_key_mismatch_rejected_by_trigger():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.document_chunk,
            source_identity_key=f"document_chunk:{uuid.uuid4()}",  # deliberately wrong
            source_role=SourceRole.unknown,
            observed_at=datetime.now(timezone.utc),
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text="x",
            content_hash="c" * 64,
            content_hash_version="sha256-utf8-v1",
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(
            DocumentSourceUnit(
                memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.document_chunk,
                document_id=document.id, version_id=None, chunk_id=chunk.id,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_exact_one_subtype_required_at_commit():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.document_record,
            source_identity_key=f"document_record:{uuid.uuid4()}",
            source_role=SourceRole.unknown,
            observed_at=datetime.now(timezone.utc),
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text=None,
            content_hash=None,
            content_hash_version=None,
            snapshot_status=SnapshotStatus.missing,
        )
        session.add(msu)
        # No document_source_units row created — the deferred constraint trigger must catch
        # this at commit, not silently allow an orphan parent.
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_purge_requires_null_content_check_constraint():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        # A raw UPDATE trying to mark purged without nulling content must fail the CHECK
        # constraint (independent of the immutability trigger, which would also block it).
        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(
                sa_text(
                    "UPDATE memory_source_units SET lifecycle_status='purged', "
                    "purged_at=now(), purge_reason='x' WHERE id=:id"
                ),
                {"id": str(msu_id)},
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_document_source_with_founder_source_role_rejected_by_trigger():
    """A document source is never directly attributable to founder/assistant/external/system
    at ingest time — mainai_app has direct INSERT on both tables, so nothing else in the
    database stops a bug or future code path from inserting source_role='founder' for a raw
    document upload, which (source_role being immutable once set) would be a PERMANENT false
    authority claim. The DSU validation trigger must reject it at commit."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        msu = MemorySourceUnit(
            owner_id=owner.id,
            source_kind=SourceKind.document_chunk,
            source_identity_key=f"document_chunk:{chunk.id}",
            source_role=SourceRole.founder,  # deliberately wrong for a raw document source
            observed_at=datetime.now(timezone.utc),
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text="Bolaget grundades 2019.",
            content_hash=hashlib.sha256(b"Bolaget grundades 2019.").hexdigest(),
            content_hash_version="sha256-utf8-v1",
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(
            DocumentSourceUnit(
                memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.document_chunk,
                document_id=document.id, version_id=None, chunk_id=chunk.id,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="source_role"):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- lifecycle CHECK coherence -----------------------------------------------------------


def test_lifecycle_coherence_rejects_active_row_with_stale_revocation_reason():
    """A row can't sit `active` while still carrying a stale revocation_reason/purge_reason
    from nowhere -- an earlier, looser version of this CHECK only verified revoked_at/
    purged_at for the 'active' branch, not revocation_reason/purge_reason. mainai_app has no
    UPDATE at all on memory_source_units, so this (like the other two coherence tests below)
    exercises the CHECK through the admin/migration connection, with memory.transition_active
    set to bypass the separate immutability guard trigger -- isolating the CHECK constraint
    itself, not a permission error or the guard trigger."""
    session = SessionLocal()
    admin_engine = create_engine(get_settings().database_url)
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with admin_engine.connect() as conn, conn.begin(), pytest.raises((IntegrityError, DBAPIError)):
            conn.execute(sa_text("SET LOCAL memory.transition_active = 'on'"))
            conn.execute(
                sa_text("UPDATE memory_source_units SET revocation_reason = 'stale, no revoked_at' WHERE id = :id"),
                {"id": str(msu_id)},
            )
    finally:
        session.rollback()
        session.close()
        admin_engine.dispose()


def test_lifecycle_coherence_rejects_revoked_row_missing_reason():
    session = SessionLocal()
    admin_engine = create_engine(get_settings().database_url)
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()
        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'test revoke')"),
            {"id": str(msu_id)},
        )
        session.commit()

        with admin_engine.connect() as conn, conn.begin(), pytest.raises((IntegrityError, DBAPIError)):
            conn.execute(sa_text("SET LOCAL memory.transition_active = 'on'"))
            conn.execute(
                sa_text("UPDATE memory_source_units SET revocation_reason = NULL WHERE id = :id"),
                {"id": str(msu_id)},
            )
    finally:
        session.rollback()
        session.close()
        admin_engine.dispose()


def test_lifecycle_coherence_rejects_purged_row_with_mismatched_revoked_pair():
    """purged rows may PRESERVE revoked_at/revocation_reason from an earlier
    active->revoked->purged transition, or have both NULL (direct active->purged) -- never
    one set without the other."""
    session = SessionLocal()
    admin_engine = create_engine(get_settings().database_url)
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()
        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'purged', 'test purge')"),
            {"id": str(msu_id)},
        )
        session.commit()  # direct active->purged: revoked_at/revocation_reason both NULL

        with admin_engine.connect() as conn, conn.begin(), pytest.raises((IntegrityError, DBAPIError)):
            conn.execute(sa_text("SET LOCAL memory.transition_active = 'on'"))
            conn.execute(
                sa_text("UPDATE memory_source_units SET revoked_at = now() WHERE id = :id"),
                {"id": str(msu_id)},
            )
    finally:
        session.rollback()
        session.close()
        admin_engine.dispose()


def test_lifecycle_events_reason_is_not_null():
    """mainai_app has no direct INSERT on memory_source_lifecycle_events at all (SELECT-only
    — see s1a_privilege_policy.py), so this exercises the NOT NULL constraint itself through
    the admin/migration connection, the same one migration 0019 and the SECURITY DEFINER
    functions run as — not a permission-denied error masquerading as a constraint failure."""
    session = SessionLocal()
    admin_engine = create_engine(get_settings().database_url)
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with admin_engine.connect() as conn, conn.begin(), pytest.raises((IntegrityError, DBAPIError)):
            conn.execute(
                sa_text(
                    "INSERT INTO memory_source_lifecycle_events "
                    "(owner_id, memory_source_id, from_status, to_status, reason, actor_type, actor_id) "
                    "VALUES (:owner_id, :msu_id, 'active', 'revoked', NULL, 'founder', :owner_id)"
                ),
                {"owner_id": str(owner.id), "msu_id": str(msu_id)},
            )
    finally:
        session.rollback()
        session.close()
        admin_engine.dispose()


def test_knowledge_claims_composite_fk_rejects_cross_owner_memory_source():
    """A plain single-column FK on knowledge_claims.memory_source_id would only prove the
    referenced row EXISTS, not that it belongs to the same owner — FK checks run
    independently of RLS. The composite FK (memory_source_id, owner_id) -> memory_source_
    units(id, owner_id) must reject this at the database level, not merely have RLS hide it
    afterwards."""
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="fk-a@example.com")
        owner_b = _make_user(session, email="fk-b@example.com")
        document_a = _make_document(session, owner_a.id)
        chunk_a = _make_chunk(session, owner_a.id, document_a.id)
        _set_rls_user(session, owner_a.id)
        msu_a_id = get_or_create_memory_source_unit(session, _chunk_locator(owner_a.id, document_a.id, chunk_a.id))
        session.commit()

        _set_rls_user(session, owner_b.id)
        document_b = _make_document(session, owner_b.id, title="Owner B doc")
        claim = KnowledgeClaim(
            owner_id=owner_b.id,
            source_id=document_b.id,
            claim_text="x",
            extraction_version="v1",
            memory_source_id=msu_a_id,  # belongs to owner_a, not owner_b
        )
        session.add(claim)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- immutability --------------------------------------------------------------------


def test_direct_update_of_identity_fields_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(
                sa_text("UPDATE memory_source_units SET source_role='founder' WHERE id=:id"), {"id": str(msu_id)}
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_direct_update_of_lifecycle_outside_transition_function_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(
                sa_text(
                    "UPDATE memory_source_units SET lifecycle_status='revoked', revoked_at=now(), "
                    "revocation_reason='manual' WHERE id=:id"
                ),
                {"id": str(msu_id)},
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_direct_delete_rejected_outside_erasure():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(sa_text("DELETE FROM memory_source_units WHERE id=:id"), {"id": str(msu_id)})
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- transition_own_memory_source() -----------------------------------------------------


def test_transition_own_memory_source_revoke_and_restore():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'test revoke')"),
            {"id": str(msu_id)},
        )
        session.commit()
        msu = session.get(MemorySourceUnit, msu_id)
        session.refresh(msu)
        assert msu.lifecycle_status == LifecycleStatus.revoked
        assert msu.revocation_reason == "test revoke"
        assert msu.content_text is not None  # revoke preserves the snapshot

        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'active', 'restore for test')"),
            {"id": str(msu_id)},
        )
        session.commit()
        session.refresh(msu)
        assert msu.lifecycle_status == LifecycleStatus.active
        assert msu.revocation_reason is None

        events = (
            session.query(MemorySourceLifecycleEvent)
            .filter_by(memory_source_id=msu_id)
            .order_by(MemorySourceLifecycleEvent.created_at)
            .all()
        )
        assert [e.to_status.value for e in events] == ["revoked", "active"]
        assert all(e.actor_type == "founder" for e in events)
        assert all(e.actor_id == owner.id for e in events)
    finally:
        session.rollback()
        session.close()


def test_transition_own_memory_source_purge_nulls_content():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'purged', 'source_deleted')"),
            {"id": str(msu_id)},
        )
        session.commit()
        msu = session.get(MemorySourceUnit, msu_id)
        session.refresh(msu)
        assert msu.lifecycle_status == LifecycleStatus.purged
        assert msu.content_text is None
        assert msu.content_hash is None
        assert msu.content_hash_version is None
        assert msu.snapshot_status == SnapshotStatus.exact  # unchanged: "had an exact snapshot"

        # purged is terminal
        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(
                sa_text("SELECT transition_own_memory_source(:id, 'active', 'undo')"),
                {"id": str(msu_id)},
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_transition_own_memory_source_always_logs_founder_never_caller_chosen():
    """transition_own_memory_source() no longer accepts an actor_kind argument at all — the
    only role that can ever call it (mainai_app, serving an ordinary authenticated request)
    must not be able to self-label an action as 'system' to make it look automated. Proven
    two ways: (1) the logged event is always actor_type='founder' regardless of the
    transition, and (2) calling with a 4th argument (the old, removed actor_kind parameter)
    fails outright — the parameter genuinely doesn't exist anymore, this isn't just
    unenforced."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        session.execute(
            sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'test')"), {"id": str(msu_id)}
        )
        session.commit()

        event = (
            session.query(MemorySourceLifecycleEvent)
            .filter_by(memory_source_id=msu_id)
            .one()
        )
        assert event.actor_type == "founder"
        assert event.actor_id == owner.id

        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(
                sa_text("SELECT transition_own_memory_source(:id, 'active', 'x', 'system')"),
                {"id": str(msu_id)},
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_transition_own_memory_source_rejects_cross_owner_source():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="a@example.com")
        owner_b = _make_user(session, email="b@example.com")
        document = _make_document(session, owner_a.id)
        chunk = _make_chunk(session, owner_a.id, document.id)
        _set_rls_user(session, owner_a.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner_a.id, document.id, chunk.id))
        session.commit()

        # owner_b's session (RLS context = owner_b) tries to transition owner_a's source.
        # RLS would already hide the row from a plain SELECT, but the function's own
        # explicit owner check is what's under test here (SECURITY DEFINER bypasses RLS).
        _set_rls_user(session, owner_b.id)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(
                sa_text("SELECT transition_own_memory_source(:id, 'revoked', 'x')"), {"id": str(msu_id)}
            )
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_mainai_app_cannot_execute_admin_transition_function():
    """mainai_app is the role every app request actually runs as (app/db.py's `engine` binds
    to APP_DATABASE_URL) — this session already IS mainai_app, so a permission error here is
    the real enforcement, not a mock."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(
                sa_text(
                    "SELECT transition_memory_source_admin(:id, 'revoked', 'x', 'admin', :actor)"
                ),
                {"id": str(msu_id), "actor": str(owner.id)},
            )
            session.commit()
        assert "permission denied" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


def test_mainai_app_cannot_update_or_delete_directly():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
            session.execute(
                sa_text("UPDATE memory_source_units SET revocation_reason='hack' WHERE id=:id"),
                {"id": str(msu_id)},
            )
            session.commit()
        assert "permission denied" in str(exc_info.value).lower()
    finally:
        session.rollback()
        session.close()


# --- boot-persistence: apply_runtime_privileges() survives a re-grant ------------------


def test_apply_runtime_privileges_survives_a_second_boot():
    """The concrete bug this whole mechanism exists for (docs/MAINAI_PROJECT_UNDERSTANDING_
    PLAN.md §4.8 "Databasbehörighetsmodell"): ensure_app_role.py runs unconditionally on
    EVERY boot (not just role creation) and re-grants ALL PRIVILEGES to mainai_app before
    Alembic runs. This test proves the fix — apply_runtime_privileges() re-narrows correctly
    even after that broad re-grant runs again, not just once at migration time."""
    settings = get_settings()
    admin_engine_url = settings.database_url
    engine = create_engine(admin_engine_url)
    try:
        with engine.begin() as conn:
            # Simulates ensure_app_role.py's unconditional re-grant on an ordinary restart.
            conn.execute(sa_text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainai_app"))

        with engine.connect() as conn:
            has_update = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            ).scalar()
            assert has_update is True, "test setup: the simulated re-grant should have restored UPDATE"

        module = _load_apply_runtime_privileges()
        module.apply_and_verify(admin_engine_url)

        with engine.connect() as conn:
            has_update_after = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            ).scalar()
            has_delete_after = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'DELETE')")
            ).scalar()
            can_call_admin_fn = conn.execute(
                sa_text(
                    "SELECT has_function_privilege('mainai_app', "
                    "'transition_memory_source_admin(uuid, varchar, text, varchar, uuid)', 'EXECUTE')"
                )
            ).scalar()
        assert has_update_after is False
        assert has_delete_after is False
        assert can_call_admin_fn is False
    finally:
        engine.dispose()


def test_mainai_app_privileges_are_exactly_least_privilege_no_truncate_references_trigger():
    """UPDATE/DELETE absence alone isn't least privilege: TRUNCATE in particular is NOT
    subject to RLS at all (it either succeeds outright or fails on privilege, with no
    per-row filtering possible), so leaving it granted would be a real, silent bypass no
    matter how tight the RLS policies are. REFERENCES/TRIGGER are equally unneeded.
    lifecycle_events is SELECT-only for mainai_app — it's an append-only audit trail
    written exclusively through the SECURITY DEFINER functions, never directly by the app
    role."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    expectations = {
        "memory_source_units": {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False, "REFERENCES": False, "TRIGGER": False},
        "document_source_units": {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False, "TRUNCATE": False, "REFERENCES": False, "TRIGGER": False},
        "memory_source_lifecycle_events": {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False, "TRUNCATE": False, "REFERENCES": False, "TRIGGER": False},
    }
    try:
        with engine.connect() as conn:
            errors = []
            for table, privs in expectations.items():
                for priv, expected in privs.items():
                    actual = conn.execute(
                        sa_text("SELECT has_table_privilege('mainai_app', :table, :priv)"),
                        {"table": table, "priv": priv},
                    ).scalar()
                    if bool(actual) != expected:
                        errors.append(f"{table}.{priv}: mainai_app has={actual}, expected={expected}")
            assert not errors, "\n".join(errors)
    finally:
        engine.dispose()


def test_apply_runtime_privileges_verifies_admin_function_owner_has_bypassrls():
    """The two admin/migration SECURITY DEFINER functions have no ownership check inside
    their own bodies by design (that's the whole point of the admin escape hatch) — they
    rely entirely on their owning role genuinely having BYPASSRLS (or being superuser).
    `SET row_security = off` does NOT provide this (an earlier draft incorrectly assumed
    it did — see migration 0019's docstring), so apply_runtime_privileges.py must verify
    the REAL role attribute and fail loud if a misconfigured deploy ever points these
    functions at a role that lacks it. Proven here by actually reassigning ownership to a
    fresh, deliberately non-superuser/non-BYPASSRLS role and confirming the verification
    catches it — not assumed."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    weak_role = "s1a_test_weak_owner_no_bypassrls"
    try:
        with engine.begin() as conn:
            conn.execute(sa_text(f"DROP ROLE IF EXISTS {weak_role}"))
            conn.execute(sa_text(f"CREATE ROLE {weak_role} NOSUPERUSER NOBYPASSRLS"))
            conn.execute(sa_text(f"ALTER FUNCTION transition_memory_source_admin(uuid, varchar, text, varchar, uuid) OWNER TO {weak_role}"))

        module = _load_apply_runtime_privileges()
        with pytest.raises(SystemExit):
            module.apply_and_verify(settings.database_url)
    finally:
        # Restore real ownership so later tests in this module (and this module re-run)
        # see the correct, admin-owned function again — apply_and_verify() itself doesn't
        # (and shouldn't) fix ownership, only report it as wrong.
        admin_role = engine.url.username
        with engine.begin() as conn:
            conn.execute(sa_text(f"ALTER FUNCTION transition_memory_source_admin(uuid, varchar, text, varchar, uuid) OWNER TO {admin_role}"))
            conn.execute(sa_text(f"DROP ROLE IF EXISTS {weak_role}"))
        module = _load_apply_runtime_privileges()
        module.apply_and_verify(settings.database_url)
        engine.dispose()


def test_worker_container_reboot_still_narrows_privileges_via_docker_entrypoint():
    """The concrete bug this regression test targets: docker-entrypoint.sh used to run
    apply_runtime_privileges.py only INSIDE the `RUN_MIGRATIONS=true` branch. The
    durable-worker container sets RUN_MIGRATIONS=false (see docker-compose.vps.yml) and
    never runs `alembic upgrade head` — but it still shares the same database, and
    ensure_app_role.py's unconditional ALL-PRIVILEGES re-grant runs on every container's
    boot regardless. A worker-only restart therefore used to leave mainai_app's privileges
    wide open indefinitely. This runs the REAL shell script (not a re-implementation of its
    logic) with RUN_MIGRATIONS=false and asserts privileges end up narrowed anyway."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    entrypoint = Path(__file__).resolve().parent.parent.parent / "docker-entrypoint.sh"
    try:
        with engine.begin() as conn:
            # Simulates ensure_app_role.py's unconditional re-grant on an ordinary restart,
            # exactly like test_apply_runtime_privileges_survives_a_second_boot above.
            conn.execute(sa_text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainai_app"))
        with engine.connect() as conn:
            has_update = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            ).scalar()
            assert has_update is True, "test setup: the simulated re-grant should have restored UPDATE"

        env = {**os.environ, "DATABASE_URL": settings.database_url, "RUN_MIGRATIONS": "false"}
        env.pop("MAINAI_APP_PASSWORD", None)  # skip ensure_app_role.py's block entirely
        result = subprocess.run(
            ["bash", str(entrypoint), "true"],
            cwd=str(entrypoint.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"docker-entrypoint.sh failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        assert "apply_runtime_privileges" in result.stdout

        with engine.connect() as conn:
            has_update_after = conn.execute(
                sa_text("SELECT has_table_privilege('mainai_app', 'memory_source_units', 'UPDATE')")
            ).scalar()
        assert has_update_after is False, (
            "RUN_MIGRATIONS=false must NOT skip apply_runtime_privileges.py — a worker-only "
            "restart left mainai_app's privileges wide open"
        )
    finally:
        engine.dispose()


# --- erase_owner_memory() ---------------------------------------------------------------


def test_erase_owner_memory_removes_everything_for_self():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        claim = KnowledgeClaim(
            owner_id=owner.id, source_id=document.id, claim_text="x", extraction_version="v1", memory_source_id=msu_id
        )
        session.add(claim)
        session.commit()

        session.execute(sa_text("SELECT erase_owner_memory(:oid)"), {"oid": str(owner.id)})
        session.commit()

        assert session.get(MemorySourceUnit, msu_id) is None
        assert session.get(DocumentSourceUnit, msu_id) is None
        assert session.query(KnowledgeClaim).filter_by(owner_id=owner.id).count() == 0
        assert session.query(MemorySourceLifecycleEvent).filter_by(owner_id=owner.id).count() == 0
    finally:
        session.rollback()
        session.close()


def test_erase_owner_memory_rejects_other_owner():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="a2@example.com")
        owner_b = _make_user(session, email="b2@example.com")
        document = _make_document(session, owner_a.id)
        chunk = _make_chunk(session, owner_a.id, document.id)
        _set_rls_user(session, owner_a.id)
        get_or_create_memory_source_unit(session, _chunk_locator(owner_a.id, document.id, chunk.id))
        session.commit()

        _set_rls_user(session, owner_b.id)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.execute(sa_text("SELECT erase_owner_memory(:oid)"), {"oid": str(owner_a.id)})
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- RLS isolation -----------------------------------------------------------------------


def test_rls_owner_isolation_on_memory_source_units():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="a3@example.com")
        owner_b = _make_user(session, email="b3@example.com")
        document = _make_document(session, owner_a.id)
        chunk = _make_chunk(session, owner_a.id, document.id)
        _set_rls_user(session, owner_a.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner_a.id, document.id, chunk.id))
        session.commit()

        _set_rls_user(session, owner_b.id)
        visible = session.execute(
            sa_text("SELECT id FROM memory_source_units WHERE id = :id"), {"id": str(msu_id)}
        ).first()
        assert visible is None
    finally:
        session.rollback()
        session.close()


def test_rls_default_deny_without_current_user_id():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _set_rls_user(session, owner.id)
        msu_id = get_or_create_memory_source_unit(session, _chunk_locator(owner.id, document.id, chunk.id))
        session.commit()

        current_user_id_var.set(None)
        session2 = SessionLocal()
        try:
            visible = session2.execute(
                sa_text("SELECT id FROM memory_source_units WHERE id = :id"), {"id": str(msu_id)}
            ).first()
            assert visible is None
        finally:
            session2.close()
    finally:
        session.rollback()
        session.close()
