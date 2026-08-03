"""MainAI Runtime Truthfulness and Durable Job Foundation — see migration 0025,
app/models/mainai_job.py, app/mainai_runtime_contract.py, app/rag/mainai_jobs_service.py,
app/rag/corpus_review_job.py, app/jobs/mainai_job_lease.py, app/routers/mainai_jobs.py.

Covers, in order:
  A. MainAIExecutionResponse's own validator — the core truthfulness contract.
  B. require_capability()'s fail-closed behavior.
  C. create_job(): capability gate, input_refs validation, idempotency.
  D. get_job/list_jobs/list_job_events: RLS-backed 404 semantics, cross-owner isolation.
  E. request_cancel()/retry_job(): valid/invalid state transitions, idempotency, retry budget.
  F. Every mutation records an event AND an audit log entry.
  G. Worker claim/lease: restart-safe claiming, stale-lease reclaim, no double-claim under
     real concurrency.
  H. corpus_review_job end to end: real provider call (faked), proposal with provenance,
     restart-safe resume (skips already-reviewed documents), cancellation between documents,
     provider failure -> job failed with a safe category (never raw exception text).
  I. The founder-only API surface end to end, including unauthorized-access denial.

Real local Postgres (RLS included), matching this repo's existing convention. Only the LLM
provider is faked, never the DB or RLS."""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.jobs.mainai_job_lease import claim_next_mainai_job, renew_mainai_job_lease
from app.mainai_runtime_contract import (
    CAPABILITY_MANIFEST,
    CapabilityUnavailableError,
    ExecutionResponseMode,
    MainAIExecutionResponse,
    require_capability,
)
from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.mainai_job import (
    CANCELLABLE_MAINAI_JOB_STATUSES,
    CLAIMABLE_MAINAI_JOB_STATUSES,
    RETRYABLE_MAINAI_JOB_STATUSES,
    MainAIJob,
    MainAIJobErrorCategory,
    MainAIJobEvent,
    MainAIJobProposal,
    MainAIJobStatus,
)
from app.providers.base import ChatResult, ProviderError
from app.providers.openai_provider import OpenAIProvider
from app.rag import mainai_jobs_service as service
from app.rag.corpus_review_job import run_corpus_review_job
from app.request_context import current_user_id as current_user_id_var

EMBEDDING_DIM = get_settings().embedding_dim
FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_indexed_document(session, owner_id, *, title="Källa") -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
        status=IndexStatus.indexed,
    )
    session.add(document)
    session.commit()
    return document


def _make_chunk(session, owner_id, document_id, text_value="Innehåll att granska.") -> DocumentChunk:
    _set_rls_user(session, owner_id)
    chunk = DocumentChunk(document_id=document_id, owner_id=owner_id, chunk_index=0, text=text_value, embedding=[0.1] * EMBEDDING_DIM)
    session.add(chunk)
    session.commit()
    return chunk


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _fake_chat_ok(content="Ingenting anmärkningsvärt i detta utdrag."):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=content, provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    return _chat


def _fake_chat_permanent_error():
    async def _chat(self, messages, model, **kwargs):
        raise ProviderError("bad request", category="invalid_key")

    return _chat


# --- A/B: the runtime truthfulness contract itself -------------------------------------------


def test_execution_response_requires_job_id_for_job_backed_modes():
    with pytest.raises(ValueError):
        MainAIExecutionResponse(mode=ExecutionResponseMode.execution_started, job_id=None, message="started")


def test_execution_response_forbids_job_id_for_answer_and_proposal():
    with pytest.raises(ValueError):
        MainAIExecutionResponse(mode=ExecutionResponseMode.answer, job_id=uuid.uuid4(), message="hello")


def test_execution_response_accepts_a_real_job_id_for_every_job_backed_mode():
    job_id = uuid.uuid4()
    for mode in (
        ExecutionResponseMode.execution_started,
        ExecutionResponseMode.status,
        ExecutionResponseMode.completed,
        ExecutionResponseMode.failed,
        ExecutionResponseMode.cancelled,
    ):
        resp = MainAIExecutionResponse(mode=mode, job_id=job_id, message="ok")
        assert resp.job_id == job_id


def test_execution_response_answer_and_proposal_never_require_a_job_id():
    for mode in (ExecutionResponseMode.answer, ExecutionResponseMode.proposal):
        resp = MainAIExecutionResponse(mode=mode, job_id=None, message="ok")
        assert resp.job_id is None


def test_require_capability_fails_closed_for_unknown_capability():
    with pytest.raises(CapabilityUnavailableError):
        require_capability("delete_production_database")


def test_require_capability_accepts_every_manifest_entry():
    for capability in CAPABILITY_MANIFEST:
        require_capability(capability)  # must not raise


# --- C: create_job() ---------------------------------------------------------------------------


def test_create_job_rejects_unknown_capability_before_creating_any_row(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    with pytest.raises(service.CapabilityUnavailableError):
        service.create_job(db_session, owner_id=user.id, job_type="delete_everything", input_refs=[], created_by="founder")
    count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs")).scalar()
    assert count == 0


def test_create_job_rejects_empty_input_refs(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    with pytest.raises(service.InvalidInputRefsError):
        service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[], created_by="founder")


def test_create_job_rejects_a_document_that_is_not_yet_indexed(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    doc = Document(title="Ej klar", source=DocumentSource.upload, uploaded_by=user.id, status=IndexStatus.extracting)
    db_session.add(doc)
    db_session.commit()
    with pytest.raises(service.InvalidInputRefsError):
        service.create_job(
            db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder"
        )


def test_create_job_rejects_a_document_owned_by_someone_else(db_session, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    other_doc = _make_indexed_document(db_session, other.id)
    _set_rls_user(db_session, owner.id)
    with pytest.raises(service.InvalidInputRefsError):
        service.create_job(
            db_session,
            owner_id=owner.id,
            job_type="corpus_review",
            input_refs=[{"type": "document", "id": str(other_doc.id)}],
            created_by="founder",
        )


def test_create_job_succeeds_with_a_real_owned_indexed_document(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(
        db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder"
    )
    assert job.status == MainAIJobStatus.queued
    assert job.progress_current == 0
    assert job.retry_count == 0


def test_create_job_is_idempotent_per_owner_and_key(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    refs = [{"type": "document", "id": str(doc.id)}]
    job1 = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=refs, created_by="founder", idempotency_key="k1")
    job2 = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=refs, created_by="founder", idempotency_key="k1")
    assert job1.id == job2.id
    count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(user.id)}).scalar()
    assert count == 1


def test_create_job_different_owners_can_reuse_the_same_idempotency_key(db_session, make_verified_user):
    a, _ = make_verified_user()
    b, _ = make_verified_user()
    doc_a = _make_indexed_document(db_session, a.id)
    doc_b = _make_indexed_document(db_session, b.id)
    _set_rls_user(db_session, a.id)
    job_a = service.create_job(
        db_session, owner_id=a.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc_a.id)}], created_by="founder", idempotency_key="shared"
    )
    _set_rls_user(db_session, b.id)
    job_b = service.create_job(
        db_session, owner_id=b.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc_b.id)}], created_by="founder", idempotency_key="shared"
    )
    assert job_a.id != job_b.id


def test_create_job_writes_a_created_event_and_audit_entry(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(
        db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder"
    )
    events = superuser_db.execute(sa_text("SELECT event_type FROM mainai_job_events WHERE job_id = :j"), {"j": str(job.id)}).scalars().all()
    assert "created" in events
    audit = superuser_db.execute(
        sa_text("SELECT count(*) FROM audit_log WHERE action = 'mainai_job_created' AND entity_id = :j"), {"j": str(job.id)}
    ).scalar()
    assert audit == 1


# --- D: get_job/list_jobs/list_job_events — RLS-backed isolation -------------------------------


def test_get_job_raises_not_found_for_a_different_owners_job(db_session, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    doc = _make_indexed_document(db_session, owner.id)
    _set_rls_user(db_session, owner.id)
    job = service.create_job(db_session, owner_id=owner.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")

    _set_rls_user(db_session, other.id)
    with pytest.raises(service.JobNotFoundError):
        service.get_job(db_session, job.id)


def test_list_jobs_only_returns_the_current_owners_jobs(db_session, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    doc_owner = _make_indexed_document(db_session, owner.id)
    doc_other = _make_indexed_document(db_session, other.id)
    _set_rls_user(db_session, owner.id)
    service.create_job(db_session, owner_id=owner.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc_owner.id)}], created_by="founder")
    _set_rls_user(db_session, other.id)
    service.create_job(db_session, owner_id=other.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc_other.id)}], created_by="founder")

    _set_rls_user(db_session, owner.id)
    jobs = service.list_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].owner_id == owner.id


# --- E: cancel/retry state transitions ----------------------------------------------------------


def test_request_cancel_is_idempotent(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    service.request_cancel(db_session, job.id, requested_by=user.id)
    job2 = service.request_cancel(db_session, job.id, requested_by=user.id)
    assert job2.cancel_requested is True


def test_request_cancel_rejects_an_already_terminal_job(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    service.mark_completed(db_session, job)
    with pytest.raises(service.InvalidJobTransitionError):
        service.request_cancel(db_session, job.id, requested_by=user.id)


def test_retry_job_rejects_a_cancelled_job(db_session, make_verified_user):
    """The founder's explicit requirement: retrying must never silently override a
    cancellation."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    service.mark_cancelled(db_session, job)
    with pytest.raises(service.InvalidJobTransitionError):
        service.retry_job(db_session, job.id, requested_by=user.id)


def test_retry_job_rejects_a_completed_job(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    service.mark_completed(db_session, job)
    with pytest.raises(service.InvalidJobTransitionError):
        service.retry_job(db_session, job.id, requested_by=user.id)


def test_retry_job_succeeds_for_a_failed_job_within_budget(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    service.mark_failed(db_session, job, error_category=MainAIJobErrorCategory.transient_io)
    retried = service.retry_job(db_session, job.id, requested_by=user.id)
    assert retried.status == MainAIJobStatus.queued
    assert retried.retry_count == 1
    assert retried.error_category is None


def test_retry_job_rejects_once_retry_budget_is_exhausted(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    job.max_retries = 1
    db_session.add(job)
    db_session.commit()
    service.mark_failed(db_session, job, error_category=MainAIJobErrorCategory.transient_io)
    service.retry_job(db_session, job.id, requested_by=user.id)  # retry_count -> 1, at budget
    job = service.get_job(db_session, job.id)
    service.mark_failed(db_session, job, error_category=MainAIJobErrorCategory.transient_io)
    with pytest.raises(service.InvalidJobTransitionError):
        service.retry_job(db_session, job.id, requested_by=user.id)


def test_mark_failed_never_stores_raw_exception_text_as_public_message(db_session, make_verified_user):
    """public_message must always come from the fixed, reviewed table — never
    str(exception)."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    service.mark_failed(db_session, job, error_category=MainAIJobErrorCategory.unexpected)
    assert "Traceback" not in job.public_message
    assert job.public_message == service._PUBLIC_ERROR_MESSAGES[MainAIJobErrorCategory.unexpected]


def test_status_sets_are_disjoint_where_they_must_be():
    assert CLAIMABLE_MAINAI_JOB_STATUSES.isdisjoint(RETRYABLE_MAINAI_JOB_STATUSES)
    assert MainAIJobStatus.cancelled not in RETRYABLE_MAINAI_JOB_STATUSES
    assert MainAIJobStatus.completed not in CANCELLABLE_MAINAI_JOB_STATUSES


# --- G: worker claim/lease ------------------------------------------------------------------


def test_claim_next_mainai_job_claims_a_queued_job(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")

    claimed = claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert claimed == (job.id, user.id)

    row = superuser_db.execute(sa_text("SELECT status, locked_by FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).first()
    assert row[0] == "running"
    assert row[1] == "worker-1"


def test_claim_next_mainai_job_returns_none_when_nothing_claimable(superuser_db):
    assert claim_next_mainai_job(superuser_db, "worker-1", 120) is None


def test_claim_next_mainai_job_reclaims_an_expired_lease(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-crashed", 120)
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()

    claimed = claim_next_mainai_job(superuser_db, "worker-2", 120)
    assert claimed == (job.id, user.id)
    row = superuser_db.execute(sa_text("SELECT locked_by FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).first()
    assert row[0] == "worker-2"


def test_claim_next_mainai_job_does_not_reclaim_a_still_valid_lease(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert claim_next_mainai_job(superuser_db, "worker-2", 120) is None


def test_renew_mainai_job_lease_extends_expiry(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-1", 5)
    before = superuser_db.execute(sa_text("SELECT lease_expires_at FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).scalar()
    renew_mainai_job_lease(superuser_db, job.id, 600)
    superuser_db.commit()
    after = superuser_db.execute(sa_text("SELECT lease_expires_at FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).scalar()
    assert after > before


def test_two_workers_racing_many_jobs_never_claim_the_same_job(db_session, superuser_db, make_verified_user):
    """Real concurrency, same pattern as test_worker.py's ImportJob race test — real threads,
    two separate DB connections."""
    import threading

    user, _ = make_verified_user()
    job_ids = set()
    for i in range(15):
        doc = _make_indexed_document(db_session, user.id, title=f"doc-{i}")
        job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
        job_ids.add(job.id)

    from app.worker import _ClaimSession

    claimed_by: dict[str, list[uuid.UUID]] = {"worker-a": [], "worker-b": []}

    def _drain(worker_id: str) -> None:
        db = _ClaimSession()
        try:
            while True:
                result = claim_next_mainai_job(db, worker_id, 120)
                if result is None:
                    return
                claimed_by[worker_id].append(result[0])
        finally:
            db.close()

    threads = [threading.Thread(target=_drain, args=("worker-a",)), threading.Thread(target=_drain, args=("worker-b",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_claimed = claimed_by["worker-a"] + claimed_by["worker-b"]
    assert len(set(all_claimed)) == len(all_claimed), "no job may be claimed by both workers"
    assert set(all_claimed) == job_ids


@pytest.mark.asyncio
async def test_worker_run_once_records_a_claimed_event_and_audit_entry(db_session, superuser_db, make_verified_user, monkeypatch):
    """The founder's explicit requirement: audit events exist for create/START/cancel/retry/
    complete/fail — this proves the "start" half through the real worker poll cycle
    (Worker.run_once()), not just by calling record_claimed() directly."""
    from app.worker import Worker

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok())
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _make_chunk(db_session, user.id, doc.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    job_id = job.id

    worked = await Worker().run_once()
    assert worked is True

    events = superuser_db.execute(sa_text("SELECT event_type FROM mainai_job_events WHERE job_id = :j"), {"j": str(job_id)}).scalars().all()
    assert "claimed" in events
    audit_count = superuser_db.execute(
        sa_text("SELECT count(*) FROM audit_log WHERE action = 'mainai_job_claimed' AND entity_id = :j"), {"j": str(job_id)}
    ).scalar()
    assert audit_count == 1


# --- H: corpus_review_job end to end ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_corpus_review_job_produces_a_proposal_with_provenance(db_session, superuser_db, make_verified_user, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("Datumet i stycke 2 verkar felaktigt."))
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _make_chunk(db_session, user.id, doc.id, "Bolaget grundades 2019.")
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-1", 120)

    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed
    assert job.progress_current == 1
    assert job.progress_total == 1
    assert job.provider == "openai"

    proposals = superuser_db.execute(sa_text("SELECT source_document_id, proposal_text, status FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).all()
    assert len(proposals) == 1
    assert str(proposals[0][0]) == str(doc.id)
    assert "felaktigt" in proposals[0][1]
    assert proposals[0][2] == "proposed"


@pytest.mark.asyncio
async def test_run_corpus_review_job_never_promotes_a_proposal_to_a_knowledge_claim(db_session, superuser_db, make_verified_user, monkeypatch):
    """The founder's explicit requirement: AI interpretation is never treated as
    founder-approved truth."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok())
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _make_chunk(db_session, user.id, doc.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_seconds=120)

    claim_count = superuser_db.execute(sa_text("SELECT count(*) FROM knowledge_claims")).scalar()
    assert claim_count == 0


@pytest.mark.asyncio
async def test_run_corpus_review_job_fails_the_job_with_a_safe_category_on_provider_error(db_session, superuser_db, make_verified_user, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_permanent_error())
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _make_chunk(db_session, user.id, doc.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.failed
    assert job.error_category == MainAIJobErrorCategory.permanent.value
    assert "Traceback" not in (job.public_message or "")


@pytest.mark.asyncio
async def test_run_corpus_review_job_honors_cancel_requested_between_documents(db_session, superuser_db, make_verified_user, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok())
    user, _ = make_verified_user()
    doc1 = _make_indexed_document(db_session, user.id, title="doc-1")
    doc2 = _make_indexed_document(db_session, user.id, title="doc-2")
    _make_chunk(db_session, user.id, doc1.id)
    job = service.create_job(
        db_session,
        owner_id=user.id,
        job_type="corpus_review",
        input_refs=[{"type": "document", "id": str(doc1.id)}, {"type": "document", "id": str(doc2.id)}],
        created_by="founder",
    )
    claim_next_mainai_job(superuser_db, "worker-1", 120)
    # Cancel BEFORE the loop's first cancellation check runs.
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET cancel_requested = true WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()

    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.cancelled
    assert job.cancel_acknowledged is True
    proposal_count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()
    assert proposal_count == 0


@pytest.mark.asyncio
async def test_run_corpus_review_job_is_restart_safe_and_skips_already_reviewed_documents(db_session, superuser_db, make_verified_user, monkeypatch):
    """Simulates a worker crash after document 1's proposal committed but before the job
    finished — re-running must not re-review document 1."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok("andra granskningen"))
    user, _ = make_verified_user()
    doc1 = _make_indexed_document(db_session, user.id, title="doc-1")
    doc2 = _make_indexed_document(db_session, user.id, title="doc-2")
    _make_chunk(db_session, user.id, doc1.id)
    _make_chunk(db_session, user.id, doc2.id)
    job = service.create_job(
        db_session,
        owner_id=user.id,
        job_type="corpus_review",
        input_refs=[{"type": "document", "id": str(doc1.id)}, {"type": "document", "id": str(doc2.id)}],
        created_by="founder",
    )
    claim_next_mainai_job(superuser_db, "worker-1", 120)

    # Simulate document 1 already reviewed by a prior, crashed attempt.
    _set_rls_user(db_session, user.id)
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=user.id, source_document_id=doc1.id, proposal_type="review_finding", proposal_text="första granskningen"))
    db_session.commit()

    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed
    proposals = superuser_db.execute(sa_text("SELECT proposal_text FROM mainai_job_proposals WHERE job_id = :j ORDER BY created_at"), {"j": str(job.id)}).scalars().all()
    assert len(proposals) == 2
    assert proposals[0] == "första granskningen"
    assert proposals[1] == "andra granskningen"


# --- I: founder-only API surface end to end ---------------------------------------------------


def test_api_requires_authentication(client):
    res = client.post("/api/mainai/jobs", json={"job_type": "corpus_review", "input_refs": []})
    assert res.status_code in (401, 403)


def test_api_create_get_list_cancel_job(client, db_session, make_verified_user):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    from app.founder import FOUNDER_USER_ID

    doc = _make_indexed_document(db_session, FOUNDER_USER_ID)

    create_res = client.post(
        "/api/mainai/jobs", json={"job_type": "corpus_review", "input_refs": [{"type": "document", "id": str(doc.id)}]}, headers=headers
    )
    assert create_res.status_code == 201, create_res.text
    job_id = create_res.json()["id"]
    assert create_res.json()["status"] == "queued"

    get_res = client.get(f"/api/mainai/jobs/{job_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == job_id
    assert any(e["event_type"] == "created" for e in get_res.json()["events"])

    list_res = client.get("/api/mainai/jobs")
    assert list_res.status_code == 200
    assert any(j["id"] == job_id for j in list_res.json())

    cancel_res = client.post(f"/api/mainai/jobs/{job_id}/cancel", headers=headers)
    assert cancel_res.status_code == 200
    assert cancel_res.json()["cancel_requested"] is True


def test_api_get_returns_404_for_a_nonexistent_job(client):
    _login(client)
    res = client.get(f"/api/mainai/jobs/{uuid.uuid4()}")
    assert res.status_code == 404


def test_api_create_returns_409_for_unknown_job_type(client):
    csrf = _login(client)
    res = client.post("/api/mainai/jobs", json={"job_type": "not_a_real_capability", "input_refs": []}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 409


def test_api_create_returns_422_for_empty_input_refs(client):
    csrf = _login(client)
    res = client.post("/api/mainai/jobs", json={"job_type": "corpus_review", "input_refs": []}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 422


def test_api_cancel_returns_409_for_an_already_completed_job(client, db_session):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    from app.founder import FOUNDER_USER_ID

    doc = _make_indexed_document(db_session, FOUNDER_USER_ID)
    create_res = client.post(
        "/api/mainai/jobs", json={"job_type": "corpus_review", "input_refs": [{"type": "document", "id": str(doc.id)}]}, headers=headers
    )
    job_id = create_res.json()["id"]
    job = db_session.get(MainAIJob, uuid.UUID(job_id))
    service.mark_completed(db_session, job)

    res = client.post(f"/api/mainai/jobs/{job_id}/cancel", headers=headers)
    assert res.status_code == 409


def test_api_admin_all_lists_jobs_across_owners(client, db_session, make_verified_user):
    _login(client)
    from app.founder import FOUNDER_USER_ID

    other, _ = make_verified_user()
    doc_founder = _make_indexed_document(db_session, FOUNDER_USER_ID)
    doc_other = _make_indexed_document(db_session, other.id)
    _set_rls_user(db_session, FOUNDER_USER_ID)
    service.create_job(db_session, owner_id=FOUNDER_USER_ID, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc_founder.id)}], created_by="founder")
    _set_rls_user(db_session, other.id)
    service.create_job(db_session, owner_id=other.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc_other.id)}], created_by="founder")

    res = client.get("/api/mainai/jobs/admin/all")
    assert res.status_code == 200
    owner_ids = {j["owner_id"] for j in res.json()}
    assert len(owner_ids) >= 2
