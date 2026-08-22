"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app) -- for `capability_records`, `founder_memory_notes`, and
`diagnosis_records` (migrations 0048-0050). Mirrors tests/security/test_rls_isolation.py's
own established pattern exactly.

Found missing during the adversarial cross-stack review of migrations 0048-0050 (see
docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md): each foundation's own test file
(test_capability_reality_foundation.py, test_founder_memory_foundation.py, test_causal_
diagnosis_interface.py) uses ONLY `superuser_db`, which bypasses RLS unconditionally -- every
"owner isolation" claim in those three files was, until this file, proven only at the Python
query-filter level (does `WHERE owner_id = X` work), never proven at the database level (does
Postgres itself still refuse a cross-owner row if application code forgot to filter, or if
`app.current_user_id` were wrong). This file closes exactly that gap, through the same
restricted role and session-variable mechanism the app itself uses."""

import uuid

from sqlalchemy import text

from app.diagnosis import record_diagnosis
from app.founder_memory import record_founder_memory
from app.capability_reality.service import record_capability_observation
from app.models.capability_reality import CapabilityRecord
from app.models.diagnosis import DiagnosisRecord
from app.models.founder_memory import FounderMemoryNote


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def test_user_never_reads_another_users_founder_memory_notes(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    record_founder_memory(db_session, owner_id=user_a.id, note_type="observation", content="A's own note", idempotency_key="rls-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    record_founder_memory(db_session, owner_id=user_b.id, note_type="observation", content="B's own note", idempotency_key="rls-b")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(FounderMemoryNote).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(FounderMemoryNote).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_founder_memory_note_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(FounderMemoryNote(owner_id=user_b.id, note_type="observation", content="should never be written", idempotency_key="rls-cross-write"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by founder_memory_notes_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


def test_user_never_reads_another_users_diagnosis_records(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    record_diagnosis(db_session, owner_id=user_a.id, observation="A's own observation", idempotency_key="rls-diag-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    record_diagnosis(db_session, owner_id=user_b.id, observation="B's own observation", idempotency_key="rls-diag-b")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(DiagnosisRecord).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(DiagnosisRecord).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_diagnosis_record_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(DiagnosisRecord(owner_id=user_b.id, observation="should never be written", idempotency_key="rls-diag-cross-write"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by diagnosis_records_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


def test_user_never_reads_another_users_capability_records(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    record_capability_observation(db_session, owner_id=user_a.id, capability_key=f"rls-test.{uuid.uuid4()}", domain="test", status="planned")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    record_capability_observation(db_session, owner_id=user_b.id, capability_key=f"rls-test.{uuid.uuid4()}", domain="test", status="planned")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(CapabilityRecord).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(CapabilityRecord).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_capability_record_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(CapabilityRecord(owner_id=user_b.id, capability_key="should-never-be-written", domain="test", status="planned"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by capability_records_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


def test_no_session_variable_set_sees_none_of_the_three_new_tables(db_session, make_verified_user):
    """Same "deny by default" invariant test_rls_isolation.py's own
    test_no_session_variable_set_sees_nothing() proves for the original tables -- with no
    app.current_user_id set at all, a query against any of the three new owner-scoped tables
    returns zero rows, never an error and never someone else's data."""

    user_a, _ = make_verified_user()
    _set_rls_user(db_session, user_a.id)
    record_founder_memory(db_session, owner_id=user_a.id, note_type="observation", content="x", idempotency_key="rls-novar-fm")
    record_diagnosis(db_session, owner_id=user_a.id, observation="x", idempotency_key="rls-novar-diag")
    record_capability_observation(db_session, owner_id=user_a.id, capability_key=f"rls-novar.{uuid.uuid4()}", domain="test", status="planned")
    db_session.commit()

    db_session.execute(text("RESET app.current_user_id"))
    assert db_session.query(FounderMemoryNote).all() == []
    assert db_session.query(DiagnosisRecord).all() == []
    assert db_session.query(CapabilityRecord).all() == []
