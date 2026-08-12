"""LIFE SOURCE FOUNDATION BOOTSTRAP — hardening/attack pass (founder mandate, PR #61,
pre-merge). Real local Postgres, real concurrent threads/sessions, nothing mocked at the DB
layer — the whole point of this pass is to attack actual runtime behavior, not re-assert the
design.

Section numbers below refer to the founder's 27-section attack-pass mandate. Not every section
gets its own test module; this file covers Section 6 (counter concurrency) first, then grows.
"""

import threading

from sqlalchemy import text as sa_text

from app.db import SessionLocal
from app.rag.corpus_batch import create_batch, record_stored_original
from app.request_context import current_user_id as current_user_id_var
from app.models.user import User, UserRole
from app.security import hash_password


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="hardening-owner@example.com") -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
    session.add(user)
    session.commit()
    return user


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
