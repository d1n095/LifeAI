"""Corpus manifest (docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §E/§P, migration 0037) —
`source_import_batches` / `source_import_batch_failures` / app/rag/corpus_batch.py. Real local
Postgres, RLS exercised for real.

This is the direct test of the founder's exact required proof: a corpus batch's own counters
(not a self-reported claim) must show discovered_files == stored + failed, and
parsed + unsupported + pending == stored, before the database will even allow status=completed
-- "exact N/N completeness bevisas". Also covers duplicate counting, parser-failure visibility,
unsupported-file bookkeeping, and owner isolation.
"""

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.source_import_batch import SourceImportBatch, SourceImportBatchFailure, SourceImportBatchStatus
from app.models.user import User, UserRole
from app.rag.corpus_batch import (
    create_batch,
    mark_partial_or_failed,
    record_discovery_totals,
    record_duplicate,
    record_parse_failed,
    record_parsed,
    record_storage_failed,
    record_stored_original,
    record_unsupported,
    try_mark_completed,
)
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="corpus-owner@example.com", *, role=UserRole.founder) -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=role, email_verified=True)
    session.add(user)
    session.commit()
    return user


# --- creation / discovery -------------------------------------------------------------------


def test_create_batch_starts_discovering_with_zero_counters():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Founder Corpus Import Test")
        session.commit()

        assert batch.status == SourceImportBatchStatus.discovering
        assert batch.discovered_files == 0
        assert batch.stored_originals_done == 0
        assert batch.reconciles()  # 0 == 0 + 0 and 0 == 0, vacuously true at creation
    finally:
        session.rollback()
        session.close()


def test_record_discovery_totals_sets_denominators_and_moves_to_importing():
    session = SessionLocal()
    try:
        owner = _make_user(session, "discovery@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Discovery test")
        record_discovery_totals(session, batch, files=10, archives=1, conversations=2, messages=40, total_bytes=1024)
        session.commit()

        assert batch.status == SourceImportBatchStatus.importing
        assert batch.discovered_files == 10
        assert batch.discovered_archives == 1
        assert batch.discovered_conversations == 2
        assert batch.discovered_messages == 40
        assert batch.discovered_bytes == 1024
        assert batch.stored_originals_total == 10
        assert batch.parsed_total == 10
    finally:
        session.rollback()
        session.close()


# --- exact N/N completeness proof (founder's core requirement) ------------------------------


def test_full_batch_reconciles_and_completes_exact_n_of_n():
    session = SessionLocal()
    try:
        owner = _make_user(session, "full-batch@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Full corpus")
        record_discovery_totals(session, batch, files=5)
        for _ in range(5):
            record_stored_original(session, batch)
        for _ in range(5):
            record_parsed(session, batch)
        session.commit()

        assert batch.discovered_files == 5
        assert batch.stored_originals_done == 5
        assert batch.parsed_done == 5
        assert batch.reconciles()

        completed = try_mark_completed(session, batch)
        session.commit()

        assert completed is True
        assert batch.status == SourceImportBatchStatus.completed
        assert batch.completed_at is not None
    finally:
        session.rollback()
        session.close()


def test_incomplete_batch_cannot_be_marked_completed_python_side():
    session = SessionLocal()
    try:
        owner = _make_user(session, "incomplete-python@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Partial corpus")
        record_discovery_totals(session, batch, files=5)
        for _ in range(3):
            record_stored_original(session, batch)
        session.commit()

        completed = try_mark_completed(session, batch)
        session.commit()

        assert completed is False
        assert batch.status == SourceImportBatchStatus.importing
    finally:
        session.rollback()
        session.close()


def test_db_check_constraint_rejects_completed_status_when_counters_dont_reconcile():
    """The Python-side `reconciles()` pre-check is convenience, not the actual guarantee -- the
    DB CHECK constraint (`ck_sib_completed_reconciles`) is. A caller that bypasses
    try_mark_completed() entirely and sets status='completed' directly, with counters that
    don't add up, must still be rejected at commit."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "db-check@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Bad direct completion")
        record_discovery_totals(session, batch, files=5)
        for _ in range(2):
            record_stored_original(session, batch)  # only 2 of 5 stored, 0 failed
        batch.status = SourceImportBatchStatus.completed
        with pytest.raises(IntegrityError, match="ck_sib_completed_reconciles"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_failed_files_count_toward_completeness_not_just_stored():
    """A file that could not even be stored still counts toward "accounted for" -- completeness
    means discovered_files == stored + storage_failed, not stored == discovered."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "failed-toward-completeness@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Some failures")
        record_discovery_totals(session, batch, files=4)
        for _ in range(3):
            record_stored_original(session, batch)
        record_storage_failed(session, batch, source_ref="corrupt-file.pdf", reason="write failed: disk full")
        for _ in range(3):
            record_parsed(session, batch)
        session.commit()

        assert batch.reconciles()
        completed = try_mark_completed(session, batch)
        session.commit()
        assert completed is True
    finally:
        session.rollback()
        session.close()


# --- duplicates ---------------------------------------------------------------------------


def test_record_duplicate_counts_toward_stored_and_duplicate_count():
    session = SessionLocal()
    try:
        owner = _make_user(session, "duplicates@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="With duplicates")
        record_discovery_totals(session, batch, files=3)
        record_stored_original(session, batch)  # a genuinely new file
        record_duplicate(session, batch)  # content already existed
        record_duplicate(session, batch)
        for _ in range(3):
            record_parsed(session, batch)
        session.commit()

        assert batch.stored_originals_done == 3  # 1 new + 2 duplicates, both count as "stored"
        assert batch.duplicate_count == 2
        assert batch.reconciles()
    finally:
        session.rollback()
        session.close()


# --- parser failures / unsupported visibility ------------------------------------------------


def test_record_failed_creates_visible_failure_row_with_reason():
    session = SessionLocal()
    try:
        owner = _make_user(session, "parser-failure@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Parser failures visible")
        record_discovery_totals(session, batch, files=1)
        record_stored_original(session, batch)  # the file WAS stored; only parsing failed
        record_parse_failed(session, batch, source_ref="broken.docx", reason="parser raised: bad zip central directory", retryable=False)
        session.commit()

        assert batch.parse_failed_count == 1
        assert batch.reconciles()  # 1 == 1 + 0, and 0+0+0+1 == 1 -- both stages accounted for
        failures = session.query(SourceImportBatchFailure).filter_by(batch_id=batch.id).all()
        assert len(failures) == 1
        assert failures[0].source_ref == "broken.docx"
        assert "bad zip central directory" in failures[0].reason
        assert failures[0].retryable is False
        assert failures[0].owner_id == owner.id
    finally:
        session.rollback()
        session.close()


def test_record_unsupported_still_stored_but_never_parsed():
    session = SessionLocal()
    try:
        owner = _make_user(session, "unsupported@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Unsupported format")
        record_discovery_totals(session, batch, files=2)
        record_stored_original(session, batch)  # the unsupported file itself IS stored
        record_unsupported(session, batch)
        record_stored_original(session, batch)
        record_parsed(session, batch)
        session.commit()

        assert batch.stored_originals_done == 2
        assert batch.unsupported_count == 1
        assert batch.parsed_done == 1
        assert batch.reconciles()

        completed = try_mark_completed(session, batch)
        session.commit()
        assert completed is True
    finally:
        session.rollback()
        session.close()


# --- partial/failed status when a run ends incomplete -----------------------------------------


def test_mark_partial_when_some_sources_succeeded():
    session = SessionLocal()
    try:
        owner = _make_user(session, "partial@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Interrupted run")
        record_discovery_totals(session, batch, files=5)
        for _ in range(2):
            record_stored_original(session, batch)
        mark_partial_or_failed(session, batch)
        session.commit()

        assert batch.status == SourceImportBatchStatus.partial
    finally:
        session.rollback()
        session.close()


def test_mark_failed_when_nothing_succeeded():
    session = SessionLocal()
    try:
        owner = _make_user(session, "failed-run@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Dead on arrival")
        record_discovery_totals(session, batch, files=5)
        mark_partial_or_failed(session, batch)
        session.commit()

        assert batch.status == SourceImportBatchStatus.failed
    finally:
        session.rollback()
        session.close()


# --- CHECK constraints on raw counters --------------------------------------------------------


def test_negative_counters_rejected_by_check_constraint():
    session = SessionLocal()
    try:
        owner = _make_user(session, "negative-counter@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Bad counters")
        session.commit()
        batch.storage_failed_count = -1
        with pytest.raises(IntegrityError, match="ck_sib_counts_non_negative"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_invalid_status_rejected_by_check_constraint():
    session = SessionLocal()
    try:
        owner = _make_user(session, "invalid-status@example.com")
        _set_rls_user(session, owner.id)

        batch = create_batch(session, owner.id, label="Bad status")
        session.flush()
        with pytest.raises(IntegrityError, match="ck_sib_status"):
            session.execute(
                sa_text("UPDATE source_import_batches SET status = 'bogus_status' WHERE id = :id"),
                {"id": str(batch.id)},
            )
    finally:
        session.rollback()
        session.close()


# --- RLS isolation -----------------------------------------------------------------------


def test_rls_owner_isolation_on_source_import_batches():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, "batch-rls-a@example.com")
        owner_b = _make_user(session, "batch-rls-b@example.com")
        _set_rls_user(session, owner_a.id)
        create_batch(session, owner_a.id, label="Owner A's corpus")
        session.commit()

        _set_rls_user(session, owner_b.id)
        count = session.query(SourceImportBatch).count()
        assert count == 0
    finally:
        session.rollback()
        session.close()


def test_rls_owner_isolation_on_source_import_batch_failures():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, "failure-rls-a@example.com")
        owner_b = _make_user(session, "failure-rls-b@example.com")
        _set_rls_user(session, owner_a.id)
        batch = create_batch(session, owner_a.id, label="Owner A's corpus")
        record_storage_failed(session, batch, source_ref="a.pdf", reason="boom")
        session.commit()

        _set_rls_user(session, owner_b.id)
        count = session.query(SourceImportBatchFailure).count()
        assert count == 0
    finally:
        session.rollback()
        session.close()


def test_policy_registry_new_tables_present():
    from app.rls import POLICY_DEFINITIONS

    policy_tables = {policy["table"] for policy in POLICY_DEFINITIONS}
    assert "source_import_batches" in policy_tables
    assert "source_import_batch_failures" in policy_tables
    assert "message_source_units" in policy_tables
