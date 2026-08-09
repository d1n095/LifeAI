"""Durable production backfill-run reporting (migration 0025, docs/MAINAI_PROJECT_UNDERSTANDING_
PLAN.md §4.8) — app/rag/backfill/memory_source_run.py. Real local Postgres, RLS exercised for
real, same pattern as tests/backend/rag/test_memory_source_backfill.py.
"""

import threading
import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import KnowledgeClaim
from app.models.memory_source_backfill_run import BackfillRunMode, BackfillRunStatus, MemorySourceBackfillFailure, MemorySourceBackfillRun
from app.models.memory_source_unit import MemorySourceUnit
from app.models.user import User, UserRole
from app.rag.backfill.memory_source_run import (
    BackfillRunBusy,
    BackfillRunNotAdvanceable,
    advance_backfill_run,
    cancel_backfill_run,
    create_or_resume_backfill_run,
    get_backfill_run,
    record_failure,
)
from app.request_context import current_user_id as current_user_id_var
from app.routers.admin import _backfill_run_out
from app.security import hash_password


def _set_rls_user(session, owner_id) -> None:
    # Both halves matter: the contextvar is what app/db.py's after_begin listener re-applies
    # SET LOCAL from on every NEW transaction (needed here since backfill_memory_source_units
    # commits/rolls back per-claim, starting fresh transactions mid-call) — see
    # tests/backend/rag/test_memory_source_backfill.py's identical helper.
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email, *, role=UserRole.founder) -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=role, email_verified=True)
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


def _make_claim(session, owner_id, document_id, *, chunk_id=None, version_id=None, claim_text="Ett pastaende.") -> KnowledgeClaim:
    _set_rls_user(session, owner_id)
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=document_id, chunk_id=chunk_id, version_id=version_id,
        claim_text=claim_text, extraction_version="v1",
    )
    session.add(claim)
    session.commit()
    return claim


# --- create/resume: idempotent reruns, no duplicate execution -----------------------------


def test_create_backfill_run_snapshots_counts_and_starts_pending():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-create@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)

        assert run.status == BackfillRunStatus.pending
        assert run.mode == BackfillRunMode.real
        assert run.total_candidates_snapshot == 1
        assert run.already_done_snapshot == 0
        assert run.processed_count == 0
        assert run.started_at is None
        assert run.completed_at is None
    finally:
        session.rollback()
        session.close()


def test_create_or_resume_returns_existing_active_run_idempotent():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-idempotent@example.com")
        document = _make_document(session, owner.id)
        _make_claim(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        first = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.dry_run)
        second = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.dry_run)

        assert first.id == second.id
        count = session.query(MemorySourceBackfillRun).filter_by(owner_id=owner.id).count()
        assert count == 1
    finally:
        session.rollback()
        session.close()


def test_create_or_resume_two_concurrent_creators_converge_to_one_run():
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "run-concurrent@example.com")
        document = _make_document(setup_session, owner.id)
        _make_claim(setup_session, owner.id, document.id)
    finally:
        setup_session.close()

    results: dict = {}
    errors: dict = {}

    def _worker(name):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner.id)
            results[name] = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        except Exception as exc:  # noqa: BLE001 - captured and asserted on below
            errors[name] = exc
        finally:
            session.close()

    thread_a = threading.Thread(target=_worker, args=("a",))
    thread_b = threading.Thread(target=_worker, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, f"a worker raised unexpectedly: {errors}"
    assert results["a"].id == results["b"].id

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner.id)
        count = verify_session.query(MemorySourceBackfillRun).filter_by(owner_id=owner.id).count()
        assert count == 1
    finally:
        verify_session.close()


# --- advance: resume, final counters, bounded batches --------------------------------------


def test_advance_backfill_run_resumes_to_completion_with_consistent_final_counters():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-advance@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        for i in range(5):
            _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text=f"Claim {i}")
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real, batch_size=2)
        assert run.status == BackfillRunStatus.pending

        # Batch size 2, 5 candidates -> at least 3 advance() calls needed (bounded batches:
        # each call does exactly one batch, never more).
        seen_statuses = []
        for _ in range(10):
            run = advance_backfill_run(session, run)
            seen_statuses.append(run.status)
            if run.status != BackfillRunStatus.running:
                break

        assert run.status == BackfillRunStatus.completed
        assert run.completed_at is not None
        assert run.started_at is not None
        assert seen_statuses.count(BackfillRunStatus.running) >= 2  # never completed in one bounded call
        assert run.batches_completed >= 3

        # No fabricated completion claim: final counters sum to exactly what was processed.
        total_outcomes = (
            run.exact_chunk_count
            + run.degraded_version_count
            + run.missing_document_only_count
            + run.skipped_unresolvable_count
            + run.failed_count
        )
        assert total_outcomes == run.processed_count == 5
        assert run.exact_chunk_count == 5

        remaining = (
            session.query(KnowledgeClaim)
            .filter(KnowledgeClaim.owner_id == owner.id, KnowledgeClaim.memory_source_id.is_(None))
            .count()
        )
        assert remaining == 0
    finally:
        session.rollback()
        session.close()


def test_advance_backfill_run_dry_run_resumes_without_double_counting():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-dryrun@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        for i in range(4):
            _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text=f"Dry claim {i}")
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.dry_run, batch_size=2)

        run = advance_backfill_run(session, run)
        assert run.status == BackfillRunStatus.running
        assert run.processed_count == 2

        run = advance_backfill_run(session, run)
        assert run.processed_count == 4  # NOT 4+2=6 — the cursor must prevent re-scanning

        run = advance_backfill_run(session, run)
        assert run.status == BackfillRunStatus.completed
        assert run.processed_count == 4  # exhaustion call itself finds 0 new candidates

        # dry_run must never write memory_source_id.
        still_null = (
            session.query(KnowledgeClaim)
            .filter(KnowledgeClaim.owner_id == owner.id, KnowledgeClaim.memory_source_id.is_(None))
            .count()
        )
        assert still_null == 4
    finally:
        session.rollback()
        session.close()


def test_advance_backfill_run_records_failure_without_claim_text():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-failure@example.com")
        doc_a = _make_document(session, owner.id, title="Dokument A")
        doc_b = _make_document(session, owner.id, title="Dokument B")
        chunk_b = _make_chunk(session, owner.id, doc_b.id)
        secret_marker = "SECRET-CLAIM-TEXT-MUST-NEVER-BE-STORED"
        # chunk_id points at a chunk belonging to a DIFFERENT document than source_id —
        # _resolve_locator's structural check fails this closed.
        claim = _make_claim(session, owner.id, doc_a.id, chunk_id=chunk_b.id, claim_text=secret_marker)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        run = advance_backfill_run(session, run)
        # The unresolvable claim's memory_source_id is never set (fails closed, "leaving
        # untouched") — it would otherwise be re-selected forever by the plain `memory_
        # source_id IS NULL` filter. The cursor is what lets THIS run correctly recognize
        # exhaustion on the next call instead of looping on the same claim.
        assert run.status == BackfillRunStatus.running
        assert run.skipped_unresolvable_count == 1

        run = advance_backfill_run(session, run)
        assert run.status == BackfillRunStatus.completed
        assert run.skipped_unresolvable_count == 1
        assert run.exact_chunk_count == 0

        failures = session.query(MemorySourceBackfillFailure).filter_by(run_id=run.id).all()
        assert len(failures) == 1
        failure = failures[0]
        assert failure.claim_id == claim.id
        assert failure.owner_id == owner.id
        assert secret_marker not in failure.reason
        assert str(claim.source_id) in failure.reason or str(claim.chunk_id) in failure.reason
    finally:
        session.rollback()
        session.close()


def test_record_failure_upserts_within_same_run_not_duplicated():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-failure-upsert@example.com")
        doc_a = _make_document(session, owner.id, title="A")
        doc_b = _make_document(session, owner.id, title="B")
        chunk_b = _make_chunk(session, owner.id, doc_b.id)
        claim = _make_claim(session, owner.id, doc_a.id, chunk_id=chunk_b.id)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.dry_run, batch_size=50)
        # Two advance() calls each re-tally the same still-unresolved claim in dry-run mode
        # only if the cursor didn't advance past it — but it DOES advance, so instead we call
        # record_failure directly twice to test the upsert path in isolation.
        record_failure(session, run, claim.id, "first reason")
        record_failure(session, run, claim.id, "second reason")
        session.commit()

        rows = session.query(MemorySourceBackfillFailure).filter_by(run_id=run.id, claim_id=claim.id).all()
        assert len(rows) == 1
        assert rows[0].reason == "second reason"
        assert rows[0].attempt_count == 2
    finally:
        session.rollback()
        session.close()


# --- crash recovery / no silent retries -----------------------------------------------------


def test_advance_backfill_run_top_level_exception_marks_run_failed(monkeypatch):
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-crash@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-batch")

        import app.rag.backfill.memory_source_run as run_module

        # Patch the NAME as bound inside run_module's own namespace (a `from x import y`
        # creates a separate binding there) — patching the source module's attribute alone
        # would not affect an already-imported reference.
        monkeypatch.setattr(run_module, "backfill_memory_source_units", _boom)

        with pytest.raises(RuntimeError, match="simulated crash"):
            advance_backfill_run(session, run)

        session.refresh(run)
        assert run.status == BackfillRunStatus.failed
        assert run.error_summary is not None
        # Sanitized (founder review round 2, HIGH finding): type name only, never str(exc) —
        # see _safe_error_summary()'s docstring for why the raw message is never persisted.
        assert "simulated crash" not in run.error_summary
        assert "RuntimeError" in run.error_summary
        assert run.completed_at is not None
    finally:
        session.rollback()
        session.close()


def test_advance_backfill_run_not_advanceable_after_terminal_status():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-terminal@example.com")
        document = _make_document(session, owner.id)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        run = advance_backfill_run(session, run)  # no candidates -> completes immediately
        assert run.status == BackfillRunStatus.completed

        with pytest.raises(BackfillRunNotAdvanceable):
            advance_backfill_run(session, run)
    finally:
        session.rollback()
        session.close()


def test_cancel_backfill_run_leaves_claims_as_valid_future_candidates():
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-cancel@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        run = cancel_backfill_run(session, run)

        assert run.status == BackfillRunStatus.cancelled
        assert run.completed_at is not None
        session.refresh(claim)
        assert claim.memory_source_id is None

        with pytest.raises(BackfillRunNotAdvanceable):
            advance_backfill_run(session, run)

        # A cancelled run doesn't block creating a NEW one for the same owner.
        new_run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        assert new_run.id != run.id
        assert new_run.status == BackfillRunStatus.pending
    finally:
        session.rollback()
        session.close()


def test_get_backfill_run_is_owner_scoped():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, "run-scope-a@example.com")
        owner_b = _make_user(session, "run-scope-b@example.com")
        _set_rls_user(session, owner_a.id)
        run = create_or_resume_backfill_run(session, owner_a.id, mode=BackfillRunMode.real)

        _set_rls_user(session, owner_b.id)
        assert get_backfill_run(session, owner_b.id, run.id) is None
    finally:
        session.rollback()
        session.close()


# --- founder review, round 2: single-flight per run, lock-gap cursor freeze, completion gate,
# --- error sanitization, RLS policy drift, full cursor exposure -----------------------------


def test_concurrent_advance_calls_on_same_run_second_is_rejected_not_lost(monkeypatch):
    """BLOCKER 1: two overlapping advance() calls on the SAME run must never both proceed —
    the second must be rejected outright (BackfillRunBusy) rather than racing and silently
    losing one side's counter increment."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "run-concurrent-advance@example.com")
        document = _make_document(setup_session, owner.id)
        chunk = _make_chunk(setup_session, owner.id, document.id)
        _make_claim(setup_session, owner.id, document.id, chunk_id=chunk.id)
        owner_id = owner.id
    finally:
        setup_session.close()

    import app.rag.backfill.memory_source_run as run_module

    real_backfill = run_module.backfill_memory_source_units
    started = threading.Event()
    release = threading.Event()

    def _slow_backfill(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10), "test setup error: release was never signaled"
        return real_backfill(*args, **kwargs)

    monkeypatch.setattr(run_module, "backfill_memory_source_units", _slow_backfill)

    session_a = SessionLocal()
    errors: dict = {}
    try:
        _set_rls_user(session_a, owner_id)
        run = create_or_resume_backfill_run(session_a, owner_id, mode=BackfillRunMode.real)
        run_id = run.id

        def _worker_a():
            try:
                # ContextVars don't propagate into a NEW threading.Thread automatically (only
                # asyncio tasks inherit the creating context) -- must be re-set from INSIDE
                # this thread, same as test_create_or_resume_two_concurrent_creators_converge_
                # to_one_run's `_worker` above, or app/db.py's after_begin listener sees no
                # user id for this thread's transaction and RLS excludes the row entirely.
                _set_rls_user(session_a, owner_id)
                run_module.advance_backfill_run(session_a, run)
            except Exception as exc:  # noqa: BLE001 - captured and asserted on below
                errors["a"] = exc

        thread_a = threading.Thread(target=_worker_a)
        thread_a.start()
        assert started.wait(timeout=10), "thread_a never entered the locked section"

        session_b = SessionLocal()
        try:
            _set_rls_user(session_b, owner_id)
            run_b = get_backfill_run(session_b, owner_id, run_id)
            with pytest.raises(BackfillRunBusy):
                advance_backfill_run(session_b, run_b)
        finally:
            session_b.rollback()
            session_b.close()

        release.set()
        thread_a.join(timeout=10)
        assert not thread_a.is_alive()
        assert not errors, f"thread_a raised unexpectedly: {errors}"
    finally:
        session_a.rollback()
        session_a.close()

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        run = get_backfill_run(verify_session, owner_id, run_id)
        assert run.status == BackfillRunStatus.running  # thread_a's call processed the 1 claim
        assert run.processed_count == 1  # not lost, not doubled

        run = advance_backfill_run(verify_session, run)
        assert run.status == BackfillRunStatus.completed
        assert run.processed_count == 1
    finally:
        verify_session.close()


def test_advance_vs_cancel_race_second_caller_is_rejected(monkeypatch):
    """BLOCKER 1: a cancel() landing while an advance() batch is in flight for the SAME run
    must not be silently clobbered back to running/completed by that batch's own final
    commit — they now share the same per-run advisory lock, so the second caller is rejected
    outright instead of racing."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "run-advance-cancel-race@example.com")
        document = _make_document(setup_session, owner.id)
        chunk = _make_chunk(setup_session, owner.id, document.id)
        _make_claim(setup_session, owner.id, document.id, chunk_id=chunk.id)
        owner_id = owner.id
    finally:
        setup_session.close()

    import app.rag.backfill.memory_source_run as run_module

    real_backfill = run_module.backfill_memory_source_units
    started = threading.Event()
    release = threading.Event()

    def _slow_backfill(*args, **kwargs):
        started.set()
        assert release.wait(timeout=10), "test setup error: release was never signaled"
        return real_backfill(*args, **kwargs)

    monkeypatch.setattr(run_module, "backfill_memory_source_units", _slow_backfill)

    session_a = SessionLocal()
    errors: dict = {}
    try:
        _set_rls_user(session_a, owner_id)
        run = create_or_resume_backfill_run(session_a, owner_id, mode=BackfillRunMode.real)
        run_id = run.id

        def _worker_a():
            try:
                # See test_concurrent_advance_calls_on_same_run_second_is_rejected_not_lost's
                # identical comment: ContextVars need to be re-set from INSIDE this new thread.
                _set_rls_user(session_a, owner_id)
                run_module.advance_backfill_run(session_a, run)
            except Exception as exc:  # noqa: BLE001
                errors["a"] = exc

        thread_a = threading.Thread(target=_worker_a)
        thread_a.start()
        assert started.wait(timeout=10)

        session_b = SessionLocal()
        try:
            _set_rls_user(session_b, owner_id)
            run_b = get_backfill_run(session_b, owner_id, run_id)
            with pytest.raises(BackfillRunBusy):
                cancel_backfill_run(session_b, run_b)
        finally:
            session_b.rollback()
            session_b.close()

        release.set()
        thread_a.join(timeout=10)
        assert not thread_a.is_alive()
        assert not errors, f"thread_a raised unexpectedly: {errors}"
    finally:
        session_a.rollback()
        session_a.close()

    # advance() completed cleanly, never clobbered by the rejected cancel().
    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        run = get_backfill_run(verify_session, owner_id, run_id)
        assert run.status == BackfillRunStatus.running  # thread_a's call processed the 1 claim

        run = advance_backfill_run(verify_session, run)
        assert run.status == BackfillRunStatus.completed
    finally:
        verify_session.close()


def test_locked_claim_not_skipped_permanently_behind_cursor():
    """BLOCKER 2: a claim that's momentarily locked by an unrelated transaction must not be
    silently skipped past the persisted cursor — it must remain reachable by a later call once
    the lock clears."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "run-locked-claim@example.com")
        document = _make_document(setup_session, owner.id)
        chunk = _make_chunk(setup_session, owner.id, document.id)
        locked_claim = _make_claim(setup_session, owner.id, document.id, chunk_id=chunk.id, claim_text="Locked")
        free_claim = _make_claim(setup_session, owner.id, document.id, chunk_id=chunk.id, claim_text="Free")
        locked_claim_id, free_claim_id, owner_id = locked_claim.id, free_claim.id, owner.id
    finally:
        setup_session.close()

    lock_session = SessionLocal()
    run_id = None
    try:
        _set_rls_user(lock_session, owner_id)
        lock_session.query(KnowledgeClaim).filter(KnowledgeClaim.id == locked_claim_id).with_for_update().one()

        advance_session = SessionLocal()
        try:
            _set_rls_user(advance_session, owner_id)
            run = create_or_resume_backfill_run(advance_session, owner_id, mode=BackfillRunMode.real, batch_size=10)
            run_id = run.id
            run = advance_backfill_run(advance_session, run)

            assert run.status == BackfillRunStatus.running
            # Frozen BEFORE the locked claim's position (which sorts first) -- never advanced.
            assert run.last_cursor_claim_id is None
            assert run.last_cursor_created_at is None

            refreshed_free = advance_session.get(KnowledgeClaim, free_claim_id)
            refreshed_locked = advance_session.get(KnowledgeClaim, locked_claim_id)
            assert refreshed_free.memory_source_id is not None
            assert refreshed_locked.memory_source_id is None
        finally:
            advance_session.rollback()
            advance_session.close()
    finally:
        lock_session.rollback()  # releases the row lock
        lock_session.close()

    # Lock released -- the previously-locked claim must now be reachable and get processed
    # (this call), and a FOLLOWING call must reach genuine completion (never stuck forever
    # behind a frozen cursor). Two calls, same as every other "reach completion" test in this
    # file -- a call that just processed a real candidate correctly stays `running`; only a
    # call finding zero candidates may complete (see module docstring's "no fabricated
    # completion claims").
    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        run = get_backfill_run(verify_session, owner_id, run_id)
        run = advance_backfill_run(verify_session, run)
        assert run.status == BackfillRunStatus.running
        final_locked_claim = verify_session.get(KnowledgeClaim, locked_claim_id)
        assert final_locked_claim.memory_source_id is not None

        run = advance_backfill_run(verify_session, run)
        assert run.status == BackfillRunStatus.completed
        assert run.processed_count == 2
    finally:
        verify_session.close()


def test_advance_backfill_run_never_completes_while_locked_candidate_remains():
    """BLOCKER 2: if the ONLY remaining candidate is momentarily locked, a batch that finds
    zero selectable rows must NOT be treated as genuine exhaustion — `_real_candidates_remain()`
    must keep the run `running`, never fabricate `completed`."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "run-locked-completion@example.com")
        document = _make_document(setup_session, owner.id)
        chunk = _make_chunk(setup_session, owner.id, document.id)
        claim = _make_claim(setup_session, owner.id, document.id, chunk_id=chunk.id)
        claim_id, owner_id = claim.id, owner.id
    finally:
        setup_session.close()

    lock_session = SessionLocal()
    run_id = None
    try:
        _set_rls_user(lock_session, owner_id)
        lock_session.query(KnowledgeClaim).filter(KnowledgeClaim.id == claim_id).with_for_update().one()

        advance_session = SessionLocal()
        try:
            _set_rls_user(advance_session, owner_id)
            run = create_or_resume_backfill_run(advance_session, owner_id, mode=BackfillRunMode.real)
            run_id = run.id
            run = advance_backfill_run(advance_session, run)

            assert run.status == BackfillRunStatus.running  # NOT completed
            assert run.processed_count == 0
            assert run.completed_at is None
        finally:
            advance_session.rollback()
            advance_session.close()
    finally:
        lock_session.rollback()
        lock_session.close()

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        run = get_backfill_run(verify_session, owner_id, run_id)
        run = advance_backfill_run(verify_session, run)
        assert run.status == BackfillRunStatus.running  # this call processed the real claim

        run = advance_backfill_run(verify_session, run)
        assert run.status == BackfillRunStatus.completed
    finally:
        verify_session.close()


def test_error_summary_never_contains_raw_exception_text(monkeypatch):
    """HIGH: run.error_summary must never embed str(exc) — only the exception type name,
    bounded in length. A raw exception string could carry SQL text, bound parameters, or other
    backend internals."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-error-sanitize@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)

        sensitive_marker = "SELECT secret_column FROM some_table WHERE token='sk-live-should-never-leak'"

        def _boom(*args, **kwargs):
            raise RuntimeError(sensitive_marker)

        import app.rag.backfill.memory_source_run as run_module

        monkeypatch.setattr(run_module, "backfill_memory_source_units", _boom)

        with pytest.raises(RuntimeError, match="should-never-leak"):
            advance_backfill_run(session, run)

        session.refresh(run)
        assert run.status == BackfillRunStatus.failed
        assert run.error_summary is not None
        assert sensitive_marker not in run.error_summary
        assert "RuntimeError" in run.error_summary
        assert len(run.error_summary) <= 200
    finally:
        session.rollback()
        session.close()


def test_policy_registry_backfill_tables_present():
    """MEDIUM 4 sanity check at the service-test layer (the authoritative drift test lives in
    tests/backend/test_rls_policy_registry.py): the two new tables must both carry an
    owner-scoped RLS policy, not just ENABLE/FORCE."""
    from app.rls import POLICY_DEFINITIONS

    policy_tables = {policy["table"] for policy in POLICY_DEFINITIONS}
    assert "memory_source_backfill_runs" in policy_tables
    assert "memory_source_backfill_failures" in policy_tables


def test_admin_api_exposes_full_cursor_pair():
    """MEDIUM 6: the operator-visible report must expose BOTH halves of the resumable
    checkpoint, not just last_cursor_claim_id."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-cursor-api@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        for i in range(3):
            _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text=f"Cursor claim {i}")
        _set_rls_user(session, owner.id)

        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real, batch_size=1)
        run = advance_backfill_run(session, run)

        assert run.last_cursor_created_at is not None
        assert run.last_cursor_claim_id is not None

        out = _backfill_run_out(run)
        assert out.last_cursor_created_at == run.last_cursor_created_at
        assert out.last_cursor_claim_id == run.last_cursor_claim_id
    finally:
        session.rollback()
        session.close()


# --- founder review, round 3 (HIGH): per-claim transaction atomicity crash-window tests ----


def test_crash_between_two_claims_in_same_batch_leaves_run_at_exactly_first_claim(monkeypatch):
    """The one crash window this design cannot (and does not try to) eliminate: an interruption
    strictly BETWEEN two claims' own atomic commits, within the same batch/advance() call.
    Claim 1's data+counters+cursor must be fully durable; claim 2 must be completely untouched
    (still a valid NULL candidate, not counted anywhere) — no partial state for claim 2, no lost
    or duplicated credit for claim 1."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-crash-between-claims@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim_1 = _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text="First claim")
        claim_2 = _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text="Second claim")
        claim_1_id, claim_2_id, owner_id = claim_1.id, claim_2.id, owner.id
        _set_rls_user(session, owner_id)

        run = create_or_resume_backfill_run(session, owner_id, mode=BackfillRunMode.real, batch_size=10)

        import app.rag.backfill.memory_source as backfill_module

        real_resolve_locator = backfill_module._resolve_locator
        calls = {"n": 0}

        def _resolve_locator_crashes_on_second_call(db, claim):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated hard crash between claim 1's commit and claim 2's processing")
            return real_resolve_locator(db, claim)

        monkeypatch.setattr(backfill_module, "_resolve_locator", _resolve_locator_crashes_on_second_call)

        with pytest.raises(RuntimeError, match="simulated hard crash"):
            advance_backfill_run(session, run)

        session.refresh(run)
        # A raised-and-caught exception (unlike a real SIGKILL) is explicitly marked `failed` by
        # this module's own "no silent swallowing" design — the property under test (claim 1's
        # atomic commit survived exactly, claim 2 untouched) holds identically either way.
        assert run.status == BackfillRunStatus.failed
        assert run.processed_count == 1
        assert run.exact_chunk_count == 1
        assert run.last_cursor_claim_id == claim_1_id

        refreshed_claim_1 = session.get(KnowledgeClaim, claim_1_id)
        refreshed_claim_2 = session.get(KnowledgeClaim, claim_2_id)
        assert refreshed_claim_1.memory_source_id is not None
        assert refreshed_claim_2.memory_source_id is None
    finally:
        session.rollback()
        session.close()


def test_crash_after_memory_source_unit_created_before_claim_linked_leaves_nothing_durable(monkeypatch):
    """Founder's crash window 1: a real MemorySourceUnit gets created, then the process dies
    before the claim itself is linked to it. In this design nothing commits until BOTH happen
    together (get_or_create_memory_source_unit's own writes and claim.memory_source_id are the
    same uncommitted transaction _apply() only commits once, at the very end) — so this must
    resolve to a full rollback: no orphaned MemorySourceUnit, no linked claim. Unlike a real
    SIGKILL, this raised exception IS caught by _apply()'s own per-claim `except Exception`
    handler (correctly — that handler exists precisely so one bad claim doesn't take the whole
    batch down) — so the run stays `running`, records this claim as a structural failure with
    correct counters, and moves on, exactly as it should for an in-process error at this point."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-crash-unit-before-link@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        claim_id, owner_id = claim.id, owner.id
        _set_rls_user(session, owner_id)

        units_before = session.query(MemorySourceUnit).filter(MemorySourceUnit.owner_id == owner_id).count()

        run = create_or_resume_backfill_run(session, owner_id, mode=BackfillRunMode.real)

        import app.rag.backfill.memory_source as backfill_module

        real_get_or_create = backfill_module.get_or_create_memory_source_unit

        def _crashes_after_unit_created(db, locator):
            real_get_or_create(db, locator)  # the real INSERT happens, uncommitted, in-session
            raise RuntimeError("simulated crash after MemorySourceUnit created, before claim linked")

        monkeypatch.setattr(backfill_module, "get_or_create_memory_source_unit", _crashes_after_unit_created)

        run = advance_backfill_run(session, run)

        assert run.status == BackfillRunStatus.running  # per-claim failure, not a run-level crash
        assert run.processed_count == 1
        assert run.failed_count == 1

        refreshed_claim = session.get(KnowledgeClaim, claim_id)
        assert refreshed_claim.memory_source_id is None  # never linked

        units_after = session.query(MemorySourceUnit).filter(MemorySourceUnit.owner_id == owner_id).count()
        assert units_after == units_before  # no orphaned MemorySourceUnit survived the rollback

        failures = (
            session.query(MemorySourceBackfillFailure)
            .filter(MemorySourceBackfillFailure.run_id == run.id, MemorySourceBackfillFailure.claim_id == claim_id)
            .all()
        )
        assert len(failures) == 1
    finally:
        session.rollback()
        session.close()


def test_crash_after_claim_linked_before_run_counters_updated_leaves_nothing_durable(monkeypatch):
    """Founder's crash window 2: claim.memory_source_id is set in memory, then the process dies
    before the run's counters/cursor are ever applied. `_apply()` only commits once (after
    invoking `on_claim_outcome`), so this must ALSO resolve to a full rollback: the claim must
    NOT end up durably linked while the run's counters stay at zero — that exact combination
    (claim updated, counter not) is precisely the inconsistency this whole round exists to rule
    out."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-crash-link-before-counters@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        claim_id, owner_id = claim.id, owner.id
        _set_rls_user(session, owner_id)

        run = create_or_resume_backfill_run(session, owner_id, mode=BackfillRunMode.real)

        import app.rag.backfill.memory_source_run as run_module

        real_make_on_claim_outcome = run_module._make_on_claim_outcome

        def _crashing_on_claim_outcome(db, run_arg):
            def _raise(inner_db, delta):
                raise RuntimeError("simulated crash: claim linked in-memory, counters never applied")

            return _raise

        monkeypatch.setattr(run_module, "_make_on_claim_outcome", _crashing_on_claim_outcome)

        with pytest.raises(RuntimeError, match="simulated crash"):
            advance_backfill_run(session, run)

        session.refresh(run)
        assert run.status == BackfillRunStatus.failed
        assert run.processed_count == 0  # never durably incremented

        refreshed_claim = session.get(KnowledgeClaim, claim_id)
        assert refreshed_claim.memory_source_id is None  # the in-memory link never committed
    finally:
        session.rollback()
        session.close()


def test_crash_after_counters_updated_before_cursor_committed_leaves_nothing_durable(monkeypatch):
    """Founder's crash window 3: run counters mutated, then the process dies before the cursor
    (or the final commit) lands. In this design counters and cursor are mutated together, in the
    same in-memory closure call, with no commit boundary between them at all (see
    _make_on_claim_outcome's own docstring) — so the latest possible injection point for this
    exact window is right before `_assert_monotonic`'s check (both already mutated in Python,
    still fully uncommitted). Even there, an interruption must roll back BOTH together: no
    counter increment may survive without its paired cursor advance, or vice versa."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-crash-counters-before-cursor@example.com")
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        claim_id, owner_id = claim.id, owner.id
        _set_rls_user(session, owner_id)

        run = create_or_resume_backfill_run(session, owner_id, mode=BackfillRunMode.real)

        import app.rag.backfill.memory_source_run as run_module

        def _crashes_after_mutation_before_flush(run_arg, *, old, claim_id):
            # By this point (_assert_monotonic's own call site, inside _on_claim_outcome) both
            # run.processed_count/exact_chunk_count/... AND run.last_cursor_* have already been
            # mutated in Python -- exactly the latest moment "counters updated, cursor not yet
            # committed" can be represented in a design with no commit between them.
            assert run_arg.processed_count > old["processed_count"]
            assert run_arg.last_cursor_claim_id is not None
            raise RuntimeError("simulated crash: counters and cursor mutated in memory, never committed")

        monkeypatch.setattr(run_module, "_assert_monotonic", _crashes_after_mutation_before_flush)

        with pytest.raises(RuntimeError, match="simulated crash"):
            advance_backfill_run(session, run)

        session.refresh(run)
        assert run.status == BackfillRunStatus.failed
        assert run.processed_count == 0
        assert run.last_cursor_created_at is None
        assert run.last_cursor_claim_id is None

        refreshed_claim = session.get(KnowledgeClaim, claim_id)
        assert refreshed_claim.memory_source_id is None
    finally:
        session.rollback()
        session.close()


# --- founder review, round 3 (point 6): invariant enforcement ------------------------------


def test_db_check_constraint_rejects_processed_count_not_matching_outcome_sum():
    """Migration 0025's `ck_msbr_processed_count_matches_sum` is the DB-level tripwire behind
    `_make_on_claim_outcome()`'s by-construction guarantee that `processed_count` always equals
    the sum of the five outcome counters. This proves the constraint actually fires — not just
    that application code happens to maintain the invariant."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "run-check-constraint-sum@example.com")
        _set_rls_user(session, owner.id)
        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)

        run.processed_count = 5
        run.exact_chunk_count = 1  # sum of counters (1) != processed_count (5)
        session.add(run)
        with pytest.raises(IntegrityError, match="ck_msbr_processed_count_matches_sum"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_assert_monotonic_raises_on_decreased_counter():
    """Direct unit-level check of the service-layer monotonicity tripwire (point 6): a counter
    that decreases relative to its pre-mutation value must raise, naming the offending field."""
    from app.rag.backfill.memory_source_run import _assert_monotonic

    session = SessionLocal()
    try:
        owner = _make_user(session, "run-monotonic-counter@example.com")
        _set_rls_user(session, owner.id)
        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        run.processed_count = 3
        old = {
            "processed_count": 5,
            "exact_chunk_count": 0,
            "degraded_version_count": 0,
            "missing_document_only_count": 0,
            "skipped_unresolvable_count": 0,
            "failed_count": 0,
            "cursor": (None, None),
        }
        with pytest.raises(RuntimeError, match="processed_count decreased"):
            _assert_monotonic(run, old=old, claim_id=uuid.uuid4())
    finally:
        session.rollback()
        session.close()


def test_assert_monotonic_raises_on_cursor_moving_backward():
    """Direct unit-level check of the service-layer monotonicity tripwire (point 6): a cursor
    that moves backward relative to its pre-mutation value must raise."""
    from datetime import datetime, timedelta, timezone

    from app.rag.backfill.memory_source_run import _assert_monotonic

    session = SessionLocal()
    try:
        owner = _make_user(session, "run-monotonic-cursor@example.com")
        _set_rls_user(session, owner.id)
        run = create_or_resume_backfill_run(session, owner.id, mode=BackfillRunMode.real)
        later = datetime.now(timezone.utc)
        earlier = later - timedelta(hours=1)
        later_id, earlier_id = uuid.uuid4(), uuid.uuid4()
        old = {
            "processed_count": run.processed_count,
            "exact_chunk_count": run.exact_chunk_count,
            "degraded_version_count": run.degraded_version_count,
            "missing_document_only_count": run.missing_document_only_count,
            "skipped_unresolvable_count": run.skipped_unresolvable_count,
            "failed_count": run.failed_count,
            "cursor": (later, later_id),
        }
        run.last_cursor_created_at, run.last_cursor_claim_id = earlier, earlier_id
        with pytest.raises(RuntimeError, match="cursor moved backward"):
            _assert_monotonic(run, old=old, claim_id=uuid.uuid4())
    finally:
        session.rollback()
        session.close()
