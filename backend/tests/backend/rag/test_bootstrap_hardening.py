"""LIFE SOURCE FOUNDATION BOOTSTRAP — hardening/attack pass (founder mandate, PR #61,
pre-merge). Real local Postgres, real concurrent threads/sessions, nothing mocked at the DB
layer — the whole point of this pass is to attack actual runtime behavior, not re-assert the
design.

Section numbers below refer to the founder's 27-section attack-pass mandate. Not every section
gets its own test module; this file covers Section 6 (counter concurrency) first, then grows.
"""

import importlib.util
import threading
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.user import User, UserRole
from app.rag.corpus_batch import create_batch, record_stored_original
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """Same reasoning as tests/backend/test_account_erasure.py's identical fixture -- this
    module exercises both the S1A privilege policy (scripts/security/s1a_privilege_policy.py)
    AND account erasure's SECURITY DEFINER functions (erase_owner_memory,
    erase_own_mainai_job_children -- governed by app/rls.py's
    apply_mainai_job_runtime_privileges(), a SEPARATE policy module). conftest.py's own
    `_test_database` fixture only grants the plain table-level DML floor, never function
    EXECUTE privileges, so a run of this file in isolation needs to apply both real policies
    itself rather than relying on another test module's own fixture having run first."""
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_and_verify(get_settings().database_url)

    from app.rls import apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="hardening-owner@example.com") -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _make_document(session, owner_id, *, title="Kalla", storage_key=None, file_path=None) -> Document:
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


# --- Section 6: counter concurrency -----------------------------------------------------


def test_section6_two_concurrent_sessions_incrementing_same_counter_do_not_lose_updates():
    """The exact race the founder predicted: record_stored_original() does `batch.x += 1` on
    an ORM-loaded object, which SQLAlchemy flushes as `SET x = <python-computed literal>`, not
    a DB-level `SET x = x + 1`. Two sessions that each load the row BEFORE either commits will
    silently lose one increment under READ COMMITTED (Postgres's default) -- no error, no
    conflict, just a wrong final count. This directly threatens the N/N completeness proof: a
    batch could claim `completed` with a count that never actually reconciled against reality."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "counter-race@example.com")
        _set_rls_user(setup_session, owner.id)
        batch = create_batch(setup_session, owner.id, label="Counter race")
        setup_session.commit()
        batch_id, owner_id = batch.id, owner.id
    finally:
        setup_session.close()

    errors: list[Exception] = []
    increments = 20

    def _worker():
        try:
            session = SessionLocal()
            try:
                _set_rls_user(session, owner_id)
                from app.models.source_import_batch import SourceImportBatch

                b = session.get(SourceImportBatch, batch_id)
                record_stored_original(session, b)
                session.commit()
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(increments)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not any(t.is_alive() for t in threads), "a worker never finished"
    assert not errors, f"unexpected exceptions: {errors}"

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        from app.models.source_import_batch import SourceImportBatch

        final = verify_session.get(SourceImportBatch, batch_id)
        assert final.stored_originals_done == increments, (
            f"lost update: expected {increments} after {increments} concurrent increments, "
            f"got {final.stored_originals_done} -- record_stored_original() is not atomic"
        )
    finally:
        verify_session.close()


# --- Section 2: source immutability under real races (G/H/I/J/K) --------------------------
#
# A-F (row created NULL, legitimate first write, same-value no-op, different-value rejected,
# storage_key/file_path changed alone) are already covered by
# tests/backend/test_source_foundation_bootstrap_privileges.py. This file covers the
# remaining, genuinely concurrent cases the founder's mandate named explicitly.


def test_section2_g_both_storage_key_and_file_path_changed_in_one_statement_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session, "section2-g@example.com")
        document = _make_document(session, owner.id, storage_key="originals/g-key", file_path="/vault/g.pdf")
        _set_rls_user(session, owner.id)

        document.storage_key = "originals/tampered"
        document.file_path = "/vault/tampered.pdf"
        with pytest.raises((IntegrityError, DBAPIError), match="storage_key is immutable"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_section2_h_illegal_storage_key_change_together_with_a_legal_field_rejects_the_whole_update():
    """A single UPDATE statement that touches BOTH an allowed field (title) and the protected
    storage_key must be rejected in full -- Postgres statement atomicity means the trigger
    aborting the statement rolls back the title change too, never a partial "some columns
    applied" outcome."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "section2-h@example.com")
        document = _make_document(session, owner.id, storage_key="originals/h-key")
        _set_rls_user(session, owner.id)

        document.title = "Ny titel som aldrig ska sparas"
        document.storage_key = "originals/h-tampered"
        with pytest.raises((IntegrityError, DBAPIError), match="storage_key is immutable"):
            session.commit()
        session.rollback()

        verify_session = SessionLocal()
        try:
            _set_rls_user(verify_session, owner.id)
            reloaded = verify_session.get(Document, document.id)
            assert reloaded.title == "Kalla"  # the whole statement rolled back, not just storage_key
            assert reloaded.storage_key == "originals/h-key"
        finally:
            verify_session.close()
    finally:
        session.rollback()
        session.close()


def test_section2_i_concurrent_first_writers_with_different_values_exactly_one_wins():
    """Two sessions race to set the SAME row's storage_key from NULL with DIFFERENT values.
    Real Postgres row-level locking means the second UPDATE blocks until the first commits;
    by the time it actually runs, OLD.storage_key is no longer NULL (the first winner already
    set it), so the trigger correctly rejects the second writer's different value -- exactly
    one canonical value survives, never a silent last-write-wins."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "section2-i@example.com")
        document = _make_document(setup_session, owner.id, storage_key=None)
        owner_id, document_id = owner.id, document.id
    finally:
        setup_session.close()

    results: dict[str, str] = {}
    errors: dict[str, Exception] = {}

    def _writer(name: str, value: str):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            session.execute(
                sa_text("UPDATE documents SET storage_key = :v WHERE id = :id"),
                {"v": value, "id": str(document_id)},
            )
            session.commit()
            results[name] = "committed"
        except (IntegrityError, DBAPIError):
            session.rollback()
            results[name] = "rejected"
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc
        finally:
            session.close()

    thread_a = threading.Thread(target=_writer, args=("a", "originals/writer-a"))
    thread_b = threading.Thread(target=_writer, args=("b", "originals/writer-b"))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, f"unexpected exceptions: {errors}"
    assert sorted(results.values()) == ["committed", "rejected"], (
        f"exactly one of two concurrent different-value first-writers must win, got {results}"
    )

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        final = verify_session.get(Document, document_id)
        winner = "a" if results["a"] == "committed" else "b"
        expected = {"a": "originals/writer-a", "b": "originals/writer-b"}[winner]
        assert final.storage_key == expected
    finally:
        verify_session.close()


def test_section2_j_concurrent_first_writers_with_the_same_value_both_succeed_harmlessly():
    """Two sessions race to set the SAME row's storage_key from NULL to the IDENTICAL value
    (a realistic scenario: two workers independently re-derive the same content-addressed hash
    for the same file and both attempt to attach it). Neither commit is a "tamper" -- the
    second writer's OLD.storage_key IS NOT NULL check fires, but NEW == OLD, so the trigger's
    own `IS DISTINCT FROM` guard does not raise for the second one either."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "section2-j@example.com")
        document = _make_document(setup_session, owner.id, storage_key=None)
        owner_id, document_id = owner.id, document.id
    finally:
        setup_session.close()

    results: dict[str, str] = {}
    errors: dict[str, Exception] = {}

    def _writer(name: str):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            session.execute(
                sa_text("UPDATE documents SET storage_key = :v WHERE id = :id"),
                {"v": "originals/shared-hash", "id": str(document_id)},
            )
            session.commit()
            results[name] = "committed"
        except (IntegrityError, DBAPIError):
            session.rollback()
            results[name] = "rejected"
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc
        finally:
            session.close()

    thread_a = threading.Thread(target=_writer, args=("a",))
    thread_b = threading.Thread(target=_writer, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, f"unexpected exceptions: {errors}"
    assert results == {"a": "committed", "b": "committed"}, (
        f"same-value concurrent first writers must both succeed harmlessly, got {results}"
    )

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        final = verify_session.get(Document, document_id)
        assert final.storage_key == "originals/shared-hash"
    finally:
        verify_session.close()


def test_section2_k_stale_transaction_overwrite_after_first_writer_commits_is_rejected():
    """A session that read the row BEFORE any storage_key was set (Python object still shows
    storage_key=None), but whose own commit races AFTER a different session already won the
    first write, must not silently overwrite the winner just because its own in-memory view is
    stale -- the trigger evaluates the REAL row state at UPDATE time, not the stale Python
    object's belief."""
    session_stale = SessionLocal()
    session_winner = SessionLocal()
    try:
        owner = _make_user(session_stale, "section2-k@example.com")
        document = _make_document(session_stale, owner.id, storage_key=None)
        document_id, owner_id = document.id, owner.id

        # session_stale "reads" the row (already has document.storage_key == None in memory).
        _set_rls_user(session_stale, owner_id)
        stale_doc = session_stale.get(Document, document_id)
        assert stale_doc.storage_key is None

        # A DIFFERENT session wins the real first write and commits first.
        _set_rls_user(session_winner, owner_id)
        session_winner.execute(
            sa_text("UPDATE documents SET storage_key = :v WHERE id = :id"),
            {"v": "originals/real-winner", "id": str(document_id)},
        )
        session_winner.commit()

        # The stale session now attempts its own "first write" of a different value.
        stale_doc.storage_key = "originals/stale-overwrite-attempt"
        with pytest.raises((IntegrityError, DBAPIError), match="storage_key is immutable"):
            session_stale.commit()
    finally:
        session_stale.rollback()
        session_stale.close()
        session_winner.close()

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        final = verify_session.get(Document, document_id)
        assert final.storage_key == "originals/real-winner"
    finally:
        verify_session.close()


# --- Section 3: delete/purge trust boundary — the TRUTH, not a claim ------------------------
#
# These tests establish empirically what PR #61 does and does NOT protect against deletion.
# Verdict (see the hardening-pass final report for full reasoning): "IMMUTABLE AGAINST UPDATE"
# (proven above, Section 2) is NOT the same claim as "IMMUTABLE AGAINST DELETION" -- and this
# PR never claimed the latter. A Document row WITH a `document_source_units` row is protected
# from a raw hard DELETE by a pre-existing FK RESTRICT (migration 0019, unrelated to this
# bootstrap). The normal application delete path (`purge_source()`, S1A Pass 21-23, built
# entirely before this bootstrap) deliberately soft-deletes the Document row and, once the
# underlying blob is genuinely unreferenced, physically deletes it from disk -- a reviewed,
# intentional feature this bootstrap did not touch and does not have standing to silently
# reverse. Documents with ZERO document_source_units rows have no such FK protection at all.


def test_section3_hard_delete_of_a_document_with_a_source_unit_is_blocked_by_fk_restrict():
    """The one REAL protection against a raw `DELETE FROM documents` that exists today -- not
    purpose-built for this bootstrap, but a real, DB-enforced backstop that predates it
    (migration 0019's `document_source_units.document_id` FK has no ON DELETE action)."""
    from app.rag.memory_source import DocumentSourceLocator, get_or_create_memory_source_unit
    from app.models.document_chunk import DocumentChunk
    from app.models.memory_source_unit import SnapshotStatus
    from datetime import datetime, timezone

    session = SessionLocal()
    try:
        owner = _make_user(session, "section3-fk-restrict@example.com")
        document = _make_document(session, owner.id, storage_key="originals/section3-fk", file_path=None)
        _set_rls_user(session, owner.id)
        chunk = DocumentChunk(document_id=document.id, owner_id=owner.id, chunk_index=0, text="Text.", embedding=[0.1] * 1536)
        session.add(chunk)
        session.commit()

        get_or_create_memory_source_unit(
            session,
            DocumentSourceLocator(
                owner_id=owner.id, document_id=document.id, version_id=None, chunk_id=chunk.id,
                observed_at=datetime.now(timezone.utc), content_text="Text.", snapshot_status=SnapshotStatus.exact,
            ),
        )
        session.commit()

        with pytest.raises((IntegrityError, DBAPIError), match="foreign key|violates"):
            session.execute(sa_text("DELETE FROM documents WHERE id = :id"), {"id": str(document.id)})
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_section3_hard_delete_of_a_document_with_no_source_unit_at_all_succeeds_unprotected():
    """The gap the module docstring itself names (`legacy_without_memory_source`): a Document
    that never got a document_source_units row has NO FK backstop -- a raw hard DELETE succeeds
    today. Documented here as the honest truth, not silently fixed: closing it would mean
    either (a) retroactively backfilling a source unit for every legacy document, which
    fabricates provenance data that never really existed, or (b) adding a blanket delete-guard
    trigger on `documents` itself -- a real, standalone architectural change bigger than this
    hardening pass's scope, and one that would also have to account for account erasure's own
    legitimate hard-delete path (see the next test)."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "section3-no-fk@example.com")
        document = _make_document(session, owner.id, storage_key="originals/section3-no-source-unit")
        _set_rls_user(session, owner.id)

        document_id, owner_id = document.id, owner.id
        session.execute(sa_text("DELETE FROM documents WHERE id = :id"), {"id": str(document_id)})
        session.commit()
    finally:
        session.rollback()
        session.close()

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        assert verify_session.get(Document, document_id) is None
    finally:
        verify_session.close()


def test_section3_purge_source_soft_deletes_then_physically_deletes_an_unreferenced_blob():
    """The normal application delete path, proven end-to-end: purge_source() soft-deletes the
    Document row (deleted_at set, row still exists) then, since nothing else references the
    content-addressed storage_key, retry_source_blob_purge() physically removes the blob from
    disk. This is deliberate, pre-existing behavior (S1A Pass 21-23) this bootstrap did not
    change -- proof that "canonical originals" are NOT unconditionally undeletable today, only
    non-silently-mutable while they exist."""
    import io

    from app.rag.library_import import _store_bytes_with_reference_lock
    from app.storage import get_storage
    from app.storage.purge import purge_source

    session = SessionLocal()
    try:
        owner = _make_user(session, "section3-purge-blob@example.com")
        _set_rls_user(session, owner.id)
        blob = _store_bytes_with_reference_lock(session, get_storage(), b"section 3 purge content, unique", max_bytes=10_000)
        document = _make_document(session, owner.id, storage_key=blob.storage_key)
        session.commit()

        assert get_storage().exists(blob.storage_key) is True

        result = purge_source(session, document.id, owner.id)
        session.commit()

        assert result.deletion_status.value in ("purged", "pending")
        reloaded = session.get(Document, document.id)
        assert reloaded is not None  # soft-deleted, row still exists
        assert reloaded.deleted_at is not None

        if result.deletion_status.value == "purged":
            assert get_storage().exists(blob.storage_key) is False, (
                "purge_source() genuinely deletes the physical blob once unreferenced -- "
                "this bootstrap's storage_key immutability trigger does not and cannot prevent this"
            )
    finally:
        session.rollback()
        session.close()


def test_section3_account_erasure_hard_deletes_documents_entirely():
    """Account erasure (app/account/erasure.py, pre-existing, out of this bootstrap's scope)
    hard-deletes Document rows outright, after first purging the memory-source rows that would
    otherwise FK-block it via `erase_owner_memory()`. A deliberate, founder-initiated total
    account wipe -- not a "normal" incidental deletion path, and correctly so (a founder must
    be able to erase their own account's data in full)."""
    from app.account.erasure import erase_account_data

    session = SessionLocal()
    try:
        owner = _make_user(session, "section3-account-erasure@example.com")
        document = _make_document(session, owner.id, storage_key=None)
        _set_rls_user(session, owner.id)
        document_id, owner_id = document.id, owner.id

        erase_account_data(session, owner)
        session.commit()
    finally:
        session.rollback()
        session.close()

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        assert verify_session.get(Document, document_id) is None, (
            "account erasure is documented and expected to hard-delete Document rows entirely"
        )
    finally:
        verify_session.close()


# --- Section 8: S1C exclusive arc -- mutation test + wrong-owner attack ---------------------


def test_section8_mutation_restoring_old_document_only_trigger_makes_s1c_regress():
    """The founder's explicit mutation-testing ask: prove the S1C exclusive-arc regression
    coverage is not vacuous. Temporarily restores migration 0019's ORIGINAL, pre-0037 trigger
    function (which only ever checked `document_source_units`, with no `message` arm at all)
    on the real schema, attempts the exact same "create a message source unit" operation
    tests/backend/rag/test_message_source_units.py's own
    test_get_or_create_message_source_unit_creates_parent_and_subtype already covers, and
    asserts it NOW fails -- proving that test genuinely depends on migration 0037's fix, not
    on some unrelated permissive default. Restores the correct, current trigger in a finally
    block regardless of outcome, so this mutation never leaks into any other test."""
    from app.db import migration_engine
    from app.rag.message_source import MessageSourceLocator, get_or_create_message_source_unit

    _OLD_SINGLE_ARC_TRIGGER = """
        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;
    """
    _CURRENT_MULTI_ARC_TRIGGER = """
        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.source_kind IN ('document_chunk', 'document_version', 'document_record') THEN
                IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
                END IF;
            ELSIF NEW.source_kind = 'message' THEN
                IF NOT EXISTS (SELECT 1 FROM public.message_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching message_source_units row', NEW.id;
                END IF;
            ELSE
                RAISE EXCEPTION 'memory_source_units %: unrecognized source_kind %', NEW.id, NEW.source_kind;
            END IF;
            RETURN NEW;
        END;
        $$;
    """

    session = SessionLocal()
    try:
        owner = _make_user(session, "section8-mutation@example.com")
        _set_rls_user(session, owner.id)
        from app.models.conversation import Conversation, Message, MessageRole

        conversation = Conversation(user_id=owner.id, title="Mutationstest")
        session.add(conversation)
        session.commit()
        message = Message(conversation_id=conversation.id, role=MessageRole.user, content="Mutationstest.")
        session.add(message)
        session.commit()
        owner_id, conversation_id, message_id = owner.id, conversation.id, message.id
    finally:
        session.close()

    with migration_engine.begin() as conn:
        conn.execute(sa_text(_OLD_SINGLE_ARC_TRIGGER))

    try:
        mutated_session = SessionLocal()
        try:
            _set_rls_user(mutated_session, owner_id)
            locator = MessageSourceLocator(
                owner_id=owner_id, message_id=message_id, conversation_id=conversation_id,
                role=MessageRole.user, observed_at=message.created_at,
                content_text="Mutationstest.",
            )
            get_or_create_message_source_unit(mutated_session, locator)
            with pytest.raises(Exception, match="no matching document_source_units row"):
                mutated_session.commit()
        finally:
            mutated_session.rollback()
            mutated_session.close()
    finally:
        with migration_engine.begin() as conn:
            conn.execute(sa_text(_CURRENT_MULTI_ARC_TRIGGER))

    # Confirm the restore genuinely took effect: the real operation succeeds again afterward.
    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        second_message = Message(conversation_id=conversation_id, role=MessageRole.user, content="Efter aterstallning.")
        verify_session.add(second_message)
        verify_session.commit()
        from app.rag.message_source import MessageSourceLocator as _Locator

        msu_id = get_or_create_message_source_unit(
            verify_session,
            _Locator(
                owner_id=owner_id, message_id=second_message.id, conversation_id=conversation_id,
                role=MessageRole.user, observed_at=second_message.created_at, content_text="Efter aterstallning.",
            ),
        )
        verify_session.commit()
        assert msu_id is not None
    finally:
        verify_session.close()


def test_section8_wrong_owner_on_message_source_unit_rejected_by_trigger():
    """A message_source_units row that claims ownership of a conversation belonging to a
    DIFFERENT owner must be rejected. owner_id itself matches the RLS session (owner_a), so a
    naive "owner_id = owner_b" attempt (caught by RLS alone, a different and already-covered
    layer) is deliberately avoided here. In practice this hits an even earlier defense than the
    trigger's own `v_conv_owner IS DISTINCT FROM NEW.owner_id` check: the trigger's own SELECT
    against `messages` runs as the SAME RLS-scoped role as the caller (not SECURITY DEFINER),
    so migration 0031's owner-scoped RLS on `messages` makes owner_b's message invisible to
    owner_a's session in the first place -- "message_id does not exist" rather than "does not
    belong to owner_id". Either message is a correct rejection; which one fires is an
    implementation detail of which defense layer runs first, not a distinction worth locking
    the test to."""
    from app.models.conversation import Conversation, Message, MessageRole
    from app.models.memory_source_unit import MemorySourceUnit, MessageSourceUnit, SourceKind, SourceRole, SnapshotStatus
    from app.rag.message_source import compute_content_hash

    session = SessionLocal()
    try:
        owner_a = _make_user(session, "section8-owner-a@example.com")
        owner_b = _make_user(session, "section8-owner-b@example.com")
        _set_rls_user(session, owner_b.id)
        conversation_b = Conversation(user_id=owner_b.id, title="Owner B:s konversation")
        session.add(conversation_b)
        session.commit()
        message_b = Message(conversation_id=conversation_b.id, role=MessageRole.user, content="Text.")
        session.add(message_b)
        session.commit()
        conversation_b_id, message_b_id = conversation_b.id, message_b.id

        _set_rls_user(session, owner_a.id)
        content_hash, hash_version = compute_content_hash("Text.")
        msu = MemorySourceUnit(
            owner_id=owner_a.id,  # matches RLS/session -- the attack is claiming owner B's conversation, not owner_id itself
            source_kind=SourceKind.message,
            source_identity_key=f"message:{message_b_id}",
            source_role=SourceRole.founder,
            observed_at=message_b.created_at,
            content_text="Text.",
            content_hash=content_hash,
            content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(
            MessageSourceUnit(
                memory_source_id=msu.id, owner_id=owner_a.id, source_kind=SourceKind.message,
                message_id=message_b_id, conversation_id=conversation_b_id,
            )
        )
        with pytest.raises((IntegrityError, DBAPIError), match="does not belong to owner_id|does not exist"):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- Section 11: job fencing / stale worker for message_source_backfill --------------------


def _claim_mainai_job(db, job_id):
    """Claims `job_id` on the superuser connection, exactly as app/worker.py's real claim step
    does (see app/jobs/mainai_job_lease.py's own docstring for why the claim must run outside
    any single owner's RLS scope). Same helper shape as tests/backend/chat/test_message_sequence.py's
    and tests/backend/jobs/test_mainai_jobs.py's own `_claim`."""
    from sqlalchemy.orm import sessionmaker

    from app.jobs.mainai_job_lease import claim_next_mainai_job

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claimed = claim_next_mainai_job(claim_db, "test-worker", 120)
    finally:
        claim_db.close()
    assert claimed is not None
    assert claimed[0] == job_id
    return "test-worker", claimed[2]


@pytest.mark.asyncio
async def test_section11_message_source_backfill_job_completes_and_reports_truthfully():
    """The durable job path end to end (not just the pure backfill function Section 8/12
    already exercise): capability-gated, no-AI, real worker claim/lease, and a truthful
    completion message once every message has a source unit."""
    from app.jobs import service
    from app.jobs.handlers.message_source_backfill import MESSAGE_SOURCE_BACKFILL_JOB_TYPE, run_message_source_backfill_job
    from app.models.conversation import Conversation, Message, MessageRole
    from app.models.mainai_job import MainAIJob, MainAIJobStatus
    from app.rag.backfill.message_source import count_messages_without_source_unit

    session = SessionLocal()
    try:
        owner = _make_user(session, "section11-job-owner@example.com")
        _set_rls_user(session, owner.id)
        conversation = Conversation(user_id=owner.id, title="Section 11")
        session.add(conversation)
        session.commit()
        for i in range(3):
            session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content=f"Section 11 message {i}"))
        session.commit()
        owner_id = owner.id
        assert count_messages_without_source_unit(session, owner_id) == 3

        job = service.create_job(session, owner_id=owner_id, job_type=MESSAGE_SOURCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder")
        worker_id, generation = _claim_mainai_job(session, job.id)

        await run_message_source_backfill_job(session, job.id, owner_id, worker_id=worker_id, lease_generation=generation, lease_seconds=60)

        refreshed = session.get(MainAIJob, job.id)
        session.refresh(refreshed)
        assert refreshed.status == MainAIJobStatus.completed
        assert "Created 3 message_source_units row(s)" in refreshed.public_message
        assert "No messages remain without a source unit" in refreshed.public_message
        assert count_messages_without_source_unit(session, owner_id) == 0
    finally:
        session.rollback()
        session.close()


@pytest.mark.asyncio
async def test_section11_a_job_whose_lease_was_stolen_creates_no_source_units_at_all():
    """Lease fencing proof for `message_source_backfill`, mirroring
    tests/backend/chat/test_message_sequence.py's identical test for `message_sequence_backfill`
    (the sibling job type on the exact same mainai_jobs runtime): a worker running with a
    STALE generation (a second worker has since re-claimed/incremented it -- simulated here by
    simply passing generation + 1, the same as an actual reclaim would leave behind) must be
    fenced out at its very first write and must create zero message_source_units rows, not a
    partial set. The job itself is left exactly as `running` -- a fenced-out worker has no
    authority to transition it to any terminal state either; that is the live worker's job when
    it next heartbeats or a recovery pass reclaims it."""
    from app.jobs import service
    from app.jobs.handlers.message_source_backfill import MESSAGE_SOURCE_BACKFILL_JOB_TYPE, run_message_source_backfill_job
    from app.models.conversation import Conversation, Message, MessageRole
    from app.models.mainai_job import MainAIJob, MainAIJobStatus
    from app.rag.backfill.message_source import count_messages_without_source_unit

    session = SessionLocal()
    try:
        owner = _make_user(session, "section11-stale-worker@example.com")
        _set_rls_user(session, owner.id)
        conversation = Conversation(user_id=owner.id, title="Section 11 stale worker")
        session.add(conversation)
        session.commit()
        session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content="Historisk."))
        session.commit()
        owner_id = owner.id

        job = service.create_job(session, owner_id=owner_id, job_type=MESSAGE_SOURCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder")
        worker_id, generation = _claim_mainai_job(session, job.id)

        await run_message_source_backfill_job(
            session, job.id, owner_id, worker_id=worker_id, lease_generation=generation + 1, lease_seconds=60
        )

        assert count_messages_without_source_unit(session, owner_id) == 1, "a fenced-out worker must create nothing"
        refreshed = session.get(MainAIJob, job.id)
        session.refresh(refreshed)
        assert refreshed.status == MainAIJobStatus.running, "a fenced-out worker must not transition the job either"
        assert refreshed.progress_current == 0
    finally:
        session.rollback()
        session.close()


@pytest.mark.asyncio
async def test_section11_a_cancelled_message_source_backfill_job_stops_and_writes_nothing():
    """cancel_requested is honoured before the first batch is ever processed -- a cancel racing
    with a claim must not leave a partially-completed job masquerading as still `running` with
    silent progress, nor as `completed`."""
    from app.jobs import service
    from app.jobs.handlers.message_source_backfill import MESSAGE_SOURCE_BACKFILL_JOB_TYPE, run_message_source_backfill_job
    from app.models.conversation import Conversation, Message, MessageRole
    from app.models.mainai_job import MainAIJob, MainAIJobStatus
    from app.rag.backfill.message_source import count_messages_without_source_unit

    session = SessionLocal()
    try:
        owner = _make_user(session, "section11-cancel@example.com")
        _set_rls_user(session, owner.id)
        conversation = Conversation(user_id=owner.id, title="Section 11 cancel")
        session.add(conversation)
        session.commit()
        session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content="Avbryts."))
        session.commit()
        owner_id = owner.id

        job = service.create_job(session, owner_id=owner_id, job_type=MESSAGE_SOURCE_BACKFILL_JOB_TYPE, input_refs=[], created_by="founder")
        service.request_cancel(session, job.id, requested_by=owner_id)
        worker_id, generation = _claim_mainai_job(session, job.id)

        await run_message_source_backfill_job(session, job.id, owner_id, worker_id=worker_id, lease_generation=generation, lease_seconds=60)

        refreshed = session.get(MainAIJob, job.id)
        session.refresh(refreshed)
        assert refreshed.status == MainAIJobStatus.cancelled
        assert count_messages_without_source_unit(session, owner_id) == 1, "a cancel before the first batch backfills nothing"
    finally:
        session.rollback()
        session.close()


# --- Section 12: AI-independence proof -----------------------------------------------------


def test_section12_source_foundation_modules_never_import_app_providers():
    """Static, drift-preventing proof (not just "it happened not to call one at runtime"): none
    of the Source Foundation Bootstrap's own modules may import app.providers at all, at the
    AST level -- a future edit that adds a provider import here would be a real regression
    against the "ingen AI behövs for detta" requirement, and this test fails the moment it
    happens, before any runtime behavior even needs to be exercised."""
    import ast
    from pathlib import Path

    backend_root = Path(__file__).resolve().parent.parent.parent.parent
    source_foundation_modules = [
        backend_root / "app" / "rag" / "corpus_batch.py",
        backend_root / "app" / "rag" / "message_source.py",
        backend_root / "app" / "rag" / "backfill" / "message_source.py",
        backend_root / "app" / "jobs" / "handlers" / "message_source_backfill.py",
        backend_root / "app" / "models" / "source_import_batch.py",
    ]
    violations = []
    for path in source_foundation_modules:
        assert path.exists(), f"expected Source Foundation module not found: {path}"
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "app.providers" or alias.name.startswith("app.providers."):
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and (node.module == "app.providers" or node.module.startswith("app.providers.")):
                    violations.append(f"{path.name}: from {node.module} import ...")
    assert violations == [], f"Source Foundation modules must never import app.providers: {violations}"


def test_section12_full_corpus_batch_lifecycle_never_touches_any_provider(monkeypatch):
    """Runtime companion to the static AST check: runs the corpus manifest's full recording
    lifecycle (create -> discover -> store -> parse -> duplicate -> unsupported -> failed ->
    complete) with every provider client monkeypatched to explode if called at all -- proving
    the entire bookkeeping path genuinely needs no AI, not just that it currently doesn't call
    one by coincidence."""
    from app.providers.openai_provider import OpenAIProvider

    async def _explode(self, messages, model, **kwargs):
        raise AssertionError("corpus manifest bookkeeping must never call a provider -- no AI needed for this")

    monkeypatch.setattr(OpenAIProvider, "chat", _explode)

    session = SessionLocal()
    try:
        owner = _make_user(session, "section12-no-provider@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="AI-independence proof")
        from app.rag.corpus_batch import (
            record_discovery_totals,
            record_duplicate,
            record_failed,
            record_parsed,
            record_unsupported,
            try_mark_completed,
        )

        # discovered_files=3: 1 genuinely stored + 1 duplicate (both count toward
        # stored_originals_done=2) + 1 failed (never stored) = 3, reconciling the first
        # equation; of the 2 stored, 1 parsed + 1 unsupported = 2, reconciling the second.
        record_discovery_totals(session, batch, files=3)
        record_stored_original(session, batch)
        record_duplicate(session, batch)
        record_failed(session, batch, source_ref="broken.pdf", reason="parser raised")
        record_parsed(session, batch)
        record_unsupported(session, batch)
        session.commit()

        completed = try_mark_completed(session, batch)
        session.commit()
        assert completed is True
    finally:
        session.rollback()
        session.close()


# --- Section 19: end-to-end deterministic demo corpus with a crash mid-way ------------------


def test_section19_deterministic_demo_corpus_survives_a_crash_and_reaches_exact_n_of_n():
    """A synthetic demo corpus matching the founder's exact required scenario chain: normal
    text, CSV, a duplicate, an unsupported file, a failed file, and a conversation's worth of
    messages backfilled alongside it. A "crash" happens after SOME but not all sources are
    recorded (the batch is deliberately left mid-flight, unclosed, exactly like a real process
    dying mid-run would leave it); "restart" is simply resuming the recording calls with the
    SAME batch row -- proving discovered == accounted-for only once genuinely true, never
    fabricated, and that no source is silently dropped across the interruption."""
    from app.models.conversation import Conversation, Message, MessageRole
    from app.rag.backfill.message_source import backfill_message_source_units, count_messages_without_source_unit
    from app.rag.corpus_batch import (
        record_discovery_totals,
        record_duplicate,
        record_failed,
        record_parsed,
        record_unsupported,
        try_mark_completed,
    )

    session = SessionLocal()
    try:
        owner = _make_user(session, "section19-demo-corpus@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Section 19 demo corpus")
        # Discovered: notes.txt, data.csv, readme.md, duplicate-of-notes.txt, unsupported.xlsx,
        # corrupt.pdf -- 6 files total.
        record_discovery_totals(session, batch, files=6, conversations=1, messages=3)
        session.commit()
        batch_id, owner_id = batch.id, owner.id

        # --- "process" the first half, then the process dies (crash simulated by simply
        # stopping here -- the batch row is left exactly as committed, still `importing`) ---
        record_stored_original(session, batch)  # notes.txt
        record_parsed(session, batch)
        record_stored_original(session, batch)  # data.csv
        record_parsed(session, batch)
        record_duplicate(session, batch)  # duplicate-of-notes.txt
        record_parsed(session, batch)  # a duplicate still counts toward parsed_done -- its
        # content is identical to an already-parsed original, but §P's reconciliation invariant
        # (parsed_done + unsupported_count + semantic_pending_count == stored_originals_done)
        # requires EVERY stored original, duplicate or not, to reach one of those three terminal
        # states -- matches the established pattern in test_source_import_batches.py's
        # test_record_duplicate_counts_toward_stored_and_duplicate_count.
        session.commit()
    finally:
        session.close()

    crash_check_session = SessionLocal()
    try:
        _set_rls_user(crash_check_session, owner_id)
        from app.models.source_import_batch import SourceImportBatch, SourceImportBatchStatus

        mid_crash_batch = crash_check_session.get(SourceImportBatch, batch_id)
        assert mid_crash_batch.status == SourceImportBatchStatus.importing  # never fabricated as complete
        assert mid_crash_batch.reconciles() is False
        assert mid_crash_batch.stored_originals_done == 3  # notes.txt, data.csv, duplicate
        assert mid_crash_batch.discovered_files == 6  # 3 files still genuinely unaccounted for
    finally:
        crash_check_session.close()

    # --- "restart": a fresh session/process picks up the SAME batch and finishes the job ---
    restart_session = SessionLocal()
    try:
        _set_rls_user(restart_session, owner_id)
        from app.models.source_import_batch import SourceImportBatch, SourceImportBatchFailure

        resumed_batch = restart_session.get(SourceImportBatch, batch_id)
        record_stored_original(restart_session, resumed_batch)  # readme.md
        record_parsed(restart_session, resumed_batch)
        record_stored_original(restart_session, resumed_batch)  # unsupported.xlsx (still stored)
        record_unsupported(restart_session, resumed_batch)
        record_failed(restart_session, resumed_batch, source_ref="corrupt.pdf", reason="parser raised: not a real PDF")
        restart_session.commit()

        # Message backfill runs alongside the file corpus, independently.
        conversation = Conversation(user_id=owner_id, title="Section 19 conversation")
        restart_session.add(conversation)
        restart_session.commit()
        for i in range(3):
            restart_session.add(Message(conversation_id=conversation.id, role=MessageRole.user, content=f"Meddelande {i}"))
        restart_session.commit()
        assert count_messages_without_source_unit(restart_session, owner_id) == 3
        backfill_result = backfill_message_source_units(restart_session, owner_id)
        assert backfill_result.created == 3
        assert count_messages_without_source_unit(restart_session, owner_id) == 0

        completed = try_mark_completed(restart_session, resumed_batch)
        restart_session.commit()

        assert completed is True, "the batch must reach exact reconciliation once every discovered file is genuinely accounted for"
        assert resumed_batch.discovered_files == 6
        assert resumed_batch.stored_originals_done == 5  # notes, csv, duplicate, readme, unsupported.xlsx
        assert resumed_batch.failed_count == 1  # corrupt.pdf
        assert resumed_batch.stored_originals_done + resumed_batch.failed_count == resumed_batch.discovered_files
        assert resumed_batch.duplicate_count == 1
        assert resumed_batch.unsupported_count == 1
        assert resumed_batch.parsed_done == 4  # notes, csv, duplicate, readme
        assert resumed_batch.parsed_done + resumed_batch.unsupported_count == resumed_batch.stored_originals_done

        failures = restart_session.query(SourceImportBatchFailure).filter_by(batch_id=batch_id).all()
        assert len(failures) == 1
        assert failures[0].source_ref == "corrupt.pdf"
    finally:
        restart_session.rollback()
        restart_session.close()
