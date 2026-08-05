"""S1A deterministic backfill (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8, phase 2) —
app/rag/memory_source_backfill.py. Real local Postgres, RLS exercised for real, same pattern
as tests/backend/test_memory_source_units.py and tests/backend/test_claims.py.
"""

import threading

import pytest
from sqlalchemy import text as sa_text

from app.db import SessionLocal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import KnowledgeClaim
from app.models.knowledge_version import KnowledgeVersion
from app.models.memory_source_unit import DocumentSourceUnit, MemorySourceUnit, SnapshotStatus, SourceKind
from app.models.user import User, UserRole
from app.rag.memory_source_backfill import backfill_memory_source_units
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="backfill-owner@example.com", *, role=UserRole.founder) -> User:
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


# --- resolution order --------------------------------------------------------------------


def test_backfill_chunk_backed_exact_uses_real_chunk_text():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id, text_value="Den riktiga chunktexten.")
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)

        assert result.candidates_total == 1
        assert result.chunk_backed == 1
        assert result.version_backed == 0
        assert result.document_backed == 0
        assert result.skipped_mismatch == 0
        assert result.failed == 0

        session.refresh(claim)
        assert claim.memory_source_id is not None
        msu = session.get(MemorySourceUnit, claim.memory_source_id)
        assert msu.source_kind == SourceKind.document_chunk
        assert msu.snapshot_status == SnapshotStatus.exact
        assert msu.content_text == "Den riktiga chunktexten."
        dsu = session.get(DocumentSourceUnit, claim.memory_source_id)
        assert dsu.chunk_id == chunk.id
    finally:
        session.rollback()
        session.close()


def test_backfill_falls_back_to_document_version_when_no_chunk():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        _set_rls_user(session, owner.id)
        version = KnowledgeVersion(
            source_id=document.id, owner_id=owner.id, version_number=1, checksum="a" * 64, extraction_version="v1"
        )
        session.add(version)
        session.flush()
        session.commit()
        claim = _make_claim(session, owner.id, document.id, version_id=version.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)

        assert result.version_backed == 1
        assert result.chunk_backed == 0
        assert result.document_backed == 0

        session.refresh(claim)
        msu = session.get(MemorySourceUnit, claim.memory_source_id)
        assert msu.source_kind == SourceKind.document_version
        assert msu.snapshot_status == SnapshotStatus.degraded
        assert msu.content_text is None
        assert msu.content_hash is None
    finally:
        session.rollback()
        session.close()


def test_backfill_falls_back_to_document_record_when_no_chunk_or_version():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        claim = _make_claim(session, owner.id, document.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)

        assert result.document_backed == 1
        assert result.chunk_backed == 0
        assert result.version_backed == 0

        session.refresh(claim)
        msu = session.get(MemorySourceUnit, claim.memory_source_id)
        assert msu.source_kind == SourceKind.document_record
        assert msu.snapshot_status == SnapshotStatus.missing
        assert msu.content_text is None
    finally:
        session.rollback()
        session.close()


def test_backfill_multiple_claims_same_chunk_share_one_source_unit():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim_a = _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text="Claim A")
        claim_b = _make_claim(session, owner.id, document.id, chunk_id=chunk.id, claim_text="Claim B")
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)

        assert result.chunk_backed == 2
        session.refresh(claim_a)
        session.refresh(claim_b)
        assert claim_a.memory_source_id == claim_b.memory_source_id
        assert session.query(MemorySourceUnit).filter_by(owner_id=owner.id).count() == 1
    finally:
        session.rollback()
        session.close()


# --- fail-closed on mismatch ---------------------------------------------------------------


def test_backfill_fails_closed_on_chunk_owned_by_different_owner():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="a-backfill@example.com")
        owner_b = _make_user(session, email="b-backfill@example.com")
        document_a = _make_document(session, owner_a.id, title="Doc A")
        document_b = _make_document(session, owner_b.id, title="Doc B")
        chunk_b = _make_chunk(session, owner_b.id, document_b.id)

        # A claim owned by owner_a, but whose chunk_id points at owner_b's chunk -- the
        # knowledge_claims.chunk_id FK has no owner-anchoring, so this is constructible even
        # though it should never happen from real extraction/backfill code.
        claim = _make_claim(session, owner_a.id, document_a.id, chunk_id=chunk_b.id)
        _set_rls_user(session, owner_a.id)

        result = backfill_memory_source_units(session, owner_a.id)

        assert result.skipped_mismatch == 1
        assert result.chunk_backed == 0
        assert result.failed == 0
        session.refresh(claim)
        assert claim.memory_source_id is None
        assert session.query(MemorySourceUnit).filter_by(owner_id=owner_a.id).count() == 0
    finally:
        session.rollback()
        session.close()


def test_backfill_fails_closed_on_chunk_belonging_to_a_different_document():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document_a = _make_document(session, owner.id, title="Doc A")
        document_b = _make_document(session, owner.id, title="Doc B")
        chunk_b = _make_chunk(session, owner.id, document_b.id)

        claim = _make_claim(session, owner.id, document_a.id, chunk_id=chunk_b.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)

        assert result.skipped_mismatch == 1
        session.refresh(claim)
        assert claim.memory_source_id is None
    finally:
        session.rollback()
        session.close()


def test_backfill_fails_closed_on_version_belonging_to_different_document():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document_a = _make_document(session, owner.id, title="Doc A")
        document_b = _make_document(session, owner.id, title="Doc B")
        _set_rls_user(session, owner.id)
        version_b = KnowledgeVersion(
            source_id=document_b.id, owner_id=owner.id, version_number=1, checksum="b" * 64, extraction_version="v1"
        )
        session.add(version_b)
        session.commit()

        claim = _make_claim(session, owner.id, document_a.id, version_id=version_b.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)

        assert result.skipped_mismatch == 1
        session.refresh(claim)
        assert claim.memory_source_id is None
    finally:
        session.rollback()
        session.close()


def test_backfill_mismatch_does_not_block_other_claims_in_same_batch():
    """A single bad claim (mismatch) must never abort or lose progress on the OTHER, valid
    claims in the same backfill call -- each claim commits independently."""
    session = SessionLocal()
    try:
        owner_a = _make_user(session, email="a-batch@example.com")
        owner_b = _make_user(session, email="b-batch@example.com")
        document_a = _make_document(session, owner_a.id)
        document_b = _make_document(session, owner_b.id)
        chunk_b = _make_chunk(session, owner_b.id, document_b.id)
        chunk_a = _make_chunk(session, owner_a.id, document_a.id, text_value="Good chunk.")

        bad_claim = _make_claim(session, owner_a.id, document_a.id, chunk_id=chunk_b.id, claim_text="Bad")
        good_claim = _make_claim(session, owner_a.id, document_a.id, chunk_id=chunk_a.id, claim_text="Good")
        _set_rls_user(session, owner_a.id)

        result = backfill_memory_source_units(session, owner_a.id)

        assert result.skipped_mismatch == 1
        assert result.chunk_backed == 1
        session.refresh(bad_claim)
        session.refresh(good_claim)
        assert bad_claim.memory_source_id is None
        assert good_claim.memory_source_id is not None
    finally:
        session.rollback()
        session.close()


# --- idempotency / dry-run / batching -------------------------------------------------------


def test_backfill_rerun_is_a_no_op():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        first = backfill_memory_source_units(session, owner.id)
        assert first.candidates_total == 1
        assert first.chunk_backed == 1
        assert first.already_done == 0

        second = backfill_memory_source_units(session, owner.id)
        assert second.candidates_total == 0
        assert second.already_done == 1
        assert session.query(MemorySourceUnit).filter_by(owner_id=owner.id).count() == 1
    finally:
        session.rollback()
        session.close()


def test_backfill_dry_run_resolves_but_never_writes():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id, dry_run=True)

        assert result.candidates_total == 1
        assert result.chunk_backed == 1
        assert session.query(MemorySourceUnit).filter_by(owner_id=owner.id).count() == 0
        session.refresh(claim)
        assert claim.memory_source_id is None

        # a real run afterward still works normally -- dry-run left no partial state behind
        real = backfill_memory_source_units(session, owner.id)
        assert real.chunk_backed == 1
    finally:
        session.rollback()
        session.close()


def test_backfill_respects_max_batches_and_batch_size():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        _set_rls_user(session, owner.id)
        for i in range(7):
            _make_claim(session, owner.id, document.id, claim_text=f"Claim {i}")
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id, batch_size=3, max_batches=1)

        assert result.candidates_total == 3
        assert result.document_backed == 3
        remaining = (
            session.query(KnowledgeClaim)
            .filter(KnowledgeClaim.owner_id == owner.id, KnowledgeClaim.memory_source_id.is_(None))
            .count()
        )
        assert remaining == 4
    finally:
        session.rollback()
        session.close()


def test_backfill_rejects_non_positive_batch_size():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        with pytest.raises(ValueError, match="batch_size"):
            backfill_memory_source_units(session, owner.id, batch_size=0)
        with pytest.raises(ValueError, match="batch_size"):
            backfill_memory_source_units(session, owner.id, batch_size=-3)
    finally:
        session.rollback()
        session.close()


def test_backfill_rejects_non_positive_max_batches():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)
        with pytest.raises(ValueError, match="max_batches"):
            backfill_memory_source_units(session, owner.id, max_batches=0)
        with pytest.raises(ValueError, match="max_batches"):
            backfill_memory_source_units(session, owner.id, max_batches=-1)
    finally:
        session.rollback()
        session.close()


def test_backfill_default_max_batches_is_finite_not_run_to_completion():
    """A caller relying on the default must get a BOUNDED amount of work per call, not
    'process everything' -- the pre-Pass-19 default (None) is exactly what let the
    infinite-loop bug run unbounded instead of surfacing after one finite call. Explicit
    max_batches=None is still available for a deliberate offline run-to-completion."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        _set_rls_user(session, owner.id)
        total_claims = 25
        for i in range(total_claims):
            _make_claim(session, owner.id, document.id, claim_text=f"Bounded default claim {i}")
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id, batch_size=1)  # default max_batches

        assert result.candidates_total < total_claims
        remaining = (
            session.query(KnowledgeClaim)
            .filter(KnowledgeClaim.owner_id == owner.id, KnowledgeClaim.memory_source_id.is_(None))
            .count()
        )
        assert remaining > 0

        # explicit max_batches=None still runs to completion
        full = backfill_memory_source_units(session, owner.id, batch_size=10, max_batches=None)
        assert full.candidates_total == remaining
        assert (
            session.query(KnowledgeClaim)
            .filter(KnowledgeClaim.owner_id == owner.id, KnowledgeClaim.memory_source_id.is_(None))
            .count()
            == 0
        )
    finally:
        session.rollback()
        session.close()


def test_backfill_never_calls_any_provider(monkeypatch):
    from app.providers.openai_provider import OpenAIProvider

    async def _fail_if_called(self, messages, model, **kwargs):
        raise AssertionError("backfill must never call a provider")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)

    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id)
        _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        result = backfill_memory_source_units(session, owner.id)
        assert result.chunk_backed == 1
    finally:
        session.rollback()
        session.close()


# --- crash-safety / restart-safety ----------------------------------------------------------


def test_backfill_crash_before_commit_leaves_no_half_source(monkeypatch):
    """A failure between get_or_create_memory_source_unit's flush (uncommitted INSERT) and
    the outer db.commit() must roll back BOTH — never leave a committed MemorySourceUnit
    with no claim actually linked to it, and never leave the claim half-linked either."""
    session = SessionLocal()
    try:
        owner = _make_user(session)
        document = _make_document(session, owner.id)
        chunk = _make_chunk(session, owner.id, document.id, text_value="Crash test text.")
        claim = _make_claim(session, owner.id, document.id, chunk_id=chunk.id)
        _set_rls_user(session, owner.id)

        real_commit = session.commit
        calls = {"n": 0}

        def _boom_once():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated crash before commit")
            return real_commit()

        monkeypatch.setattr(session, "commit", _boom_once)

        result = backfill_memory_source_units(session, owner.id)
        assert result.failed == 1
        assert result.chunk_backed == 0
        assert session.query(MemorySourceUnit).filter_by(owner_id=owner.id).count() == 0
        assert session.query(DocumentSourceUnit).filter_by(owner_id=owner.id).count() == 0
        session.refresh(claim)
        assert claim.memory_source_id is None

        monkeypatch.setattr(session, "commit", real_commit)
        retry = backfill_memory_source_units(session, owner.id)
        assert retry.chunk_backed == 1
        assert session.query(MemorySourceUnit).filter_by(owner_id=owner.id).count() == 1
    finally:
        session.rollback()
        session.close()


def test_two_concurrent_workers_converge_without_duplicate_source_units():
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, email="concurrent-backfill@example.com")
        document = _make_document(setup_session, owner.id)
        chunk = _make_chunk(setup_session, owner.id, document.id, text_value="Shared chunk text.")
        for i in range(6):
            _make_claim(setup_session, owner.id, document.id, chunk_id=chunk.id, claim_text=f"Concurrent claim {i}")
    finally:
        setup_session.close()

    results: dict = {}
    errors: dict = {}

    def _worker(name):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner.id)
            results[name] = backfill_memory_source_units(session, owner.id, batch_size=10)
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

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner.id)
        msu_count = verify_session.query(MemorySourceUnit).filter_by(owner_id=owner.id).count()
        assert msu_count == 1, "two claims backed by the same chunk must converge on ONE source unit"

        linked = (
            verify_session.query(KnowledgeClaim)
            .filter(KnowledgeClaim.owner_id == owner.id, KnowledgeClaim.memory_source_id.isnot(None))
            .count()
        )
        assert linked == 6

        total_processed = results["a"].chunk_backed + results["b"].chunk_backed
        assert total_processed == 6
    finally:
        verify_session.close()
