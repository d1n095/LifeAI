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

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.jobs.mainai_job_lease import JobLeaseLostError, claim_next_mainai_job, renew_mainai_job_lease
from app.mainai_runtime_contract import (
    CAPABILITY_MANIFEST,
    CapabilityUnavailableError,
    ExecutionResponseMode,
    MainAIExecutionResponse,
    build_answer_response,
    get_capability_status,
    require_capability,
    sanitize_unverified_execution_claims,
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

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "apply_runtime_privileges.py"


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _apply_full_privilege_policy_before_this_module():
    """test_account_deletion_removes_mainai_job_data (section N below) exercises the real
    DELETE /api/account endpoint, whose erase_account_data() calls BOTH erase_owner_memory()
    (S1A, governed by scripts/s1a_privilege_policy.py via apply_runtime_privileges.py — same
    as tests/backend/test_account_erasure.py's identical fixture) AND
    erase_own_mainai_job_children() (governed separately by app/rls.py's
    apply_mainai_job_runtime_privileges()). conftest.py's session-scoped `_test_database`
    fixture applies neither. Whether this passes must not depend on some OTHER test module
    (e.g. test_account_erasure.py) having already run first in the same session and left the
    S1A grant behind — that's exactly the ordering trap this fixture closes, matching
    production's real boot sequence (app/main.py's on_startup calls both)."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)
    apply_mainai_job_runtime_privileges(migration_engine)


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


def test_build_answer_response_is_always_mode_answer_with_no_job_id():
    resp = build_answer_response("Hej, har du en fråga om dina dokument?")
    assert resp.mode == ExecutionResponseMode.answer
    assert resp.job_id is None
    assert resp.message == "Hej, har du en fråga om dina dokument?"


def test_sanitize_unverified_execution_claims_leaves_ordinary_text_untouched():
    text = "Bolaget grundades 2019 enligt dokumentet du laddade upp."
    assert sanitize_unverified_execution_claims(text) == text


def test_sanitize_unverified_execution_claims_flags_swedish_background_work_claim():
    text = "Jag arbetar med det i bakgrunden, återkommer strax."
    sanitized = sanitize_unverified_execution_claims(text)
    assert sanitized != text
    assert sanitized.startswith(text)
    assert "MainAI-obs" in sanitized


def test_sanitize_unverified_execution_claims_flags_english_job_started_claim():
    text = "The job has started and I'll let you know when it's done."
    sanitized = sanitize_unverified_execution_claims(text)
    assert "MainAI-obs" in sanitized


def test_sanitize_unverified_execution_claims_is_case_insensitive_and_idempotent():
    text = "JOBBET HAR STARTAT, vänta lite."
    once = sanitize_unverified_execution_claims(text)
    twice = sanitize_unverified_execution_claims(once)
    assert once == twice, "sanitizing an already-sanitized reply must never append the notice a second time"


def test_require_capability_fails_closed_for_unknown_capability(db_session):
    with pytest.raises(CapabilityUnavailableError) as exc_info:
        require_capability(db_session, "delete_production_database")
    assert exc_info.value.reason == "not_implemented"


def test_require_capability_accepts_every_manifest_entry_when_configured(db_session):
    """The test environment's OPENAI_API_KEY (tests/conftest.py's fake-but-real-looking
    default) makes every manifest entry's dependency configured, matching a real founder
    deployment with an actual key set."""
    for capability in CAPABILITY_MANIFEST:
        require_capability(db_session, capability)  # must not raise


def test_get_capability_status_reports_not_implemented_for_unknown_capability(db_session):
    status = get_capability_status(db_session, "delete_production_database")
    assert status.implemented is False
    assert status.configured is False
    assert status.currently_available is False
    assert status.unavailable_reason == "not_implemented"


def test_get_capability_status_reports_currently_available_when_configured(db_session):
    status = get_capability_status(db_session, "corpus_review")
    assert status.implemented is True
    assert status.configured is True
    assert status.currently_available is True
    assert status.requires_user_action is False
    assert status.unavailable_reason is None
    assert status.modifies_existing_data is False
    assert status.writes_new_records is True


def test_get_capability_status_reports_not_configured_when_no_provider_key_present(db_session, monkeypatch):
    """Founder re-review round (PR #36): the exact gap the review found -- a capability must
    not be reported as available just because it's implemented in code, if nothing is actually
    configured to execute it."""
    from app.providers.openai_provider import OpenAIProvider

    monkeypatch.setattr(OpenAIProvider, "is_configured", lambda self: False)

    status = get_capability_status(db_session, "corpus_review")
    assert status.configured is False
    assert status.currently_available is False
    assert status.requires_user_action is True
    assert status.unavailable_reason == "not_configured"


def test_create_job_fails_closed_with_not_configured_reason_when_no_provider_available(db_session, make_verified_user, monkeypatch):
    from app.providers.openai_provider import OpenAIProvider

    monkeypatch.setattr(OpenAIProvider, "is_configured", lambda self: False)
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    with pytest.raises(CapabilityUnavailableError) as exc_info:
        service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    assert exc_info.value.reason == "not_configured"
    # No job row may be created for a capability that's implemented but not currently runnable.
    count = db_session.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(user.id)}).scalar()
    assert count == 0


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


def test_create_job_concurrent_same_owner_and_key_is_race_safe(db_session, superuser_db, make_verified_user):
    """Founder re-review round (PR #36): reproduces, as a permanent regression test, the exact
    race an independent review found and this session's own manual repro confirmed -- two real
    threads, two real DB sessions, the same (owner_id, idempotency_key), racing each other. The
    OLD select-then-insert implementation let the loser hit an unhandled IntegrityError instead
    of getting the winner's job back; the SAVEPOINT-based fix must make both calls succeed
    idempotently, exactly once, with exactly one 'created' event."""
    import threading

    from app.db import SessionLocal

    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    db_session.commit()
    doc_id = doc.id
    key = f"race-{uuid.uuid4()}"

    results: list[uuid.UUID] = []
    errors: list[str] = []
    barrier = threading.Barrier(2, timeout=5)

    def _worker():
        session = SessionLocal()
        try:
            _set_rls_user(session, user.id)
            barrier.wait()
            job = service.create_job(
                session, owner_id=user.id, job_type="corpus_review",
                input_refs=[{"type": "document", "id": str(doc_id)}], created_by="founder", idempotency_key=key,
            )
            results.append(job.id)
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below, not swallowed
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_worker), threading.Thread(target=_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"both concurrent calls must succeed idempotently, got: {errors}"
    assert len(results) == 2
    assert results[0] == results[1], "both callers must get back the SAME job id"

    row_count = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o AND idempotency_key = :k"), {"o": str(user.id), "k": key}
    ).scalar()
    assert row_count == 1, "exactly one row, no duplicate created by the race"
    created_events = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_job_events WHERE job_id = :j AND event_type = 'created'"), {"j": str(results[0])}
    ).scalar()
    assert created_events == 1, "exactly one 'created' event, not one per racing caller"


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


def _claim(db, job_id) -> tuple[str, int]:
    """Test helper: claims `job_id` on the superuser connection (matching real usage — the
    worker always claims on migration_engine, see app/worker.py's _ClaimSession) and returns
    (worker_id, lease_generation) for callers that need to exercise a fencing-guarded
    mark_*/update_progress/record_* call the way corpus_review_job.py actually would, instead
    of calling those functions against a job that was never really claimed."""
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        _, _, generation = claim_next_mainai_job(claim_db, "test-worker", 120)
    finally:
        claim_db.close()
    return "test-worker", generation


def test_request_cancel_rejects_an_already_terminal_job(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_completed(db_session, job, worker_id=worker_id, lease_generation=generation)
    with pytest.raises(service.InvalidJobTransitionError):
        service.request_cancel(db_session, job.id, requested_by=user.id)


def test_retry_job_rejects_a_cancelled_job(db_session, make_verified_user):
    """The founder's explicit requirement: retrying must never silently override a
    cancellation."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_cancelled(db_session, job, worker_id=worker_id, lease_generation=generation)
    with pytest.raises(service.InvalidJobTransitionError):
        service.retry_job(db_session, job.id, requested_by=user.id)


def test_retry_job_rejects_a_completed_job(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_completed(db_session, job, worker_id=worker_id, lease_generation=generation)
    with pytest.raises(service.InvalidJobTransitionError):
        service.retry_job(db_session, job.id, requested_by=user.id)


def test_retry_job_succeeds_for_a_failed_job_within_budget(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_failed(db_session, job, worker_id=worker_id, lease_generation=generation, error_category=MainAIJobErrorCategory.transient_io)
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
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_failed(db_session, job, worker_id=worker_id, lease_generation=generation, error_category=MainAIJobErrorCategory.transient_io)
    service.retry_job(db_session, job.id, requested_by=user.id)  # retry_count -> 1, at budget
    job = service.get_job(db_session, job.id)
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_failed(db_session, job, worker_id=worker_id, lease_generation=generation, error_category=MainAIJobErrorCategory.transient_io)
    with pytest.raises(service.InvalidJobTransitionError):
        service.retry_job(db_session, job.id, requested_by=user.id)


def test_mark_failed_never_stores_raw_exception_text_as_public_message(db_session, make_verified_user):
    """public_message must always come from the fixed, reviewed table — never
    str(exception)."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    worker_id, generation = _claim(db_session, job.id)
    job = service.get_job(db_session, job.id)
    service.mark_failed(db_session, job, worker_id=worker_id, lease_generation=generation, error_category=MainAIJobErrorCategory.unexpected)
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
    assert claimed == (job.id, user.id, 1)

    row = superuser_db.execute(sa_text("SELECT status, locked_by, lease_generation FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).first()
    assert row[0] == "running"
    assert row[1] == "worker-1"
    assert row[2] == 1


def test_claim_next_mainai_job_returns_none_when_nothing_claimable(superuser_db):
    assert claim_next_mainai_job(superuser_db, "worker-1", 120) is None


def test_claim_next_mainai_job_reclaims_an_expired_lease(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    first_claim = claim_next_mainai_job(superuser_db, "worker-crashed", 120)
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()

    claimed = claim_next_mainai_job(superuser_db, "worker-2", 120)
    assert claimed == (job.id, user.id, first_claim[2] + 1)
    row = superuser_db.execute(sa_text("SELECT locked_by, lease_generation FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).first()
    assert row[0] == "worker-2"
    assert row[1] == first_claim[2] + 1


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
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 5)
    before = superuser_db.execute(sa_text("SELECT lease_expires_at FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).scalar()
    renew_mainai_job_lease(superuser_db, job.id, "worker-1", generation, 600)
    superuser_db.commit()
    after = superuser_db.execute(sa_text("SELECT lease_expires_at FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).scalar()
    assert after > before


def test_renew_mainai_job_lease_rejects_a_stale_worker_id(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    claim_next_mainai_job(superuser_db, "worker-1", 120)
    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(superuser_db, job.id, "worker-imposter", 1, 600)


def test_renew_mainai_job_lease_rejects_a_stale_generation(db_session, superuser_db, make_verified_user):
    """The exact incident this migration/module closes: a worker whose lease already expired
    and was reclaimed by someone else (a NEW generation) must not be able to renew using its
    own now-stale generation number, even if it (coincidentally or via hostname reuse) still
    presents the SAME worker_id string the new claimant also happens to use."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    _, _, first_generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()
    claim_next_mainai_job(superuser_db, "worker-1", 120)  # same worker_id string reclaims its own expired lease -- new generation
    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(superuser_db, job.id, "worker-1", first_generation, 600)


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
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)

    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

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
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    claim_count = superuser_db.execute(sa_text("SELECT count(*) FROM knowledge_claims")).scalar()
    assert claim_count == 0


@pytest.mark.asyncio
async def test_run_corpus_review_job_records_a_per_document_skip_on_provider_error_and_still_completes(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """Founder re-review round (PR #36): a provider failure for ONE document no longer fails
    the WHOLE job — it's recorded as a `document_skipped` event (reason `provider_failed`,
    safe error_category, never raw exception text) and the job still reaches `completed`,
    honestly reporting that document as not reviewed in the completion message. Mixed outcomes
    within a single run (some documents reviewed, some provider-failed) are the expected case,
    not a systemic failure — see corpus_review_job.py's module docstring."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_permanent_error())
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _make_chunk(db_session, user.id, doc.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed
    assert "1 failed" in job.public_message
    assert "Traceback" not in (job.public_message or "")

    events = superuser_db.execute(
        sa_text("SELECT event_type, detail FROM mainai_job_events WHERE job_id = :j AND event_type = 'document_skipped'"), {"j": str(job.id)}
    ).all()
    assert len(events) == 1
    detail = events[0][1]
    assert detail["reason"] == "provider_failed"
    assert detail["document_id"] == str(doc.id)
    assert detail["error_category"] == MainAIJobErrorCategory.permanent.value
    assert "Traceback" not in json.dumps(detail)

    proposal_count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()
    assert proposal_count == 0


@pytest.mark.asyncio
async def test_run_corpus_review_job_mixed_outcomes_in_one_run(db_session, superuser_db, make_verified_user, monkeypatch):
    """One run, three documents, three different outcomes: reviewed, deleted mid-run, and a
    provider failure -- exactly the "blandade utfall" scenario the founder's review asked to
    be tested explicitly, not inferred from three separate single-outcome tests."""
    calls = {"n": 0}

    async def _chat(self, messages, model, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ChatResult(content="Ser bra ut.", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})
        raise ProviderError("bad request", category="invalid_key")

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    user, _ = make_verified_user()
    doc_reviewed = _make_indexed_document(db_session, user.id, title="reviewed")
    doc_deleted = _make_indexed_document(db_session, user.id, title="deleted")
    doc_failed = _make_indexed_document(db_session, user.id, title="failed")
    _make_chunk(db_session, user.id, doc_reviewed.id)
    _make_chunk(db_session, user.id, doc_failed.id)
    deleted_id = doc_deleted.id
    job = service.create_job(
        db_session,
        owner_id=user.id,
        job_type="corpus_review",
        input_refs=[
            {"type": "document", "id": str(doc_reviewed.id)},
            {"type": "document", "id": str(doc_deleted.id)},
            {"type": "document", "id": str(doc_failed.id)},
        ],
        created_by="founder",
    )
    # Delete doc_deleted AFTER the job's own snapshot was taken (input_refs already committed)
    # but BEFORE the loop reaches it -- exactly "raderas efter snapshot men före fetch".
    superuser_db.execute(sa_text("DELETE FROM documents WHERE id = :d"), {"d": str(deleted_id)})
    superuser_db.commit()

    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.completed
    assert "Reviewed 1 of 3" in job.public_message
    assert "1 deleted" in job.public_message
    assert "1 failed" in job.public_message

    skip_events = superuser_db.execute(
        sa_text("SELECT detail FROM mainai_job_events WHERE job_id = :j AND event_type = 'document_skipped' ORDER BY created_at"), {"j": str(job.id)}
    ).all()
    reasons = {row[0]["reason"] for row in skip_events}
    assert reasons == {"deleted", "provider_failed"}

    proposals = superuser_db.execute(sa_text("SELECT source_document_id FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).all()
    assert len(proposals) == 1
    assert str(proposals[0][0]) == str(doc_reviewed.id)


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
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    # Cancel BEFORE the loop's first cancellation check runs.
    superuser_db.execute(sa_text("UPDATE mainai_jobs SET cancel_requested = true WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()

    _set_rls_user(db_session, user.id)
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

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
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)

    # Simulate document 1 already reviewed by a prior, crashed attempt.
    _set_rls_user(db_session, user.id)
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=user.id, source_document_id=doc1.id, proposal_type="review_finding", proposal_text="första granskningen"))
    db_session.commit()

    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-1", lease_generation=generation, lease_seconds=120)

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
    worker_id, generation = _claim(db_session, uuid.UUID(job_id))
    job = service.get_job(db_session, uuid.UUID(job_id))
    service.mark_completed(db_session, job, worker_id=worker_id, lease_generation=generation)

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


# --- J: composite owner FK integrity (migration 0026 — founder review correction round) ------
# The attack this closes: owner A knows owner B's job_id (job IDs aren't secret — they appear
# in URLs). Before migration 0026, mainai_job_events/mainai_job_proposals had two
# *independent* FKs (job_id -> mainai_jobs.id, owner_id -> users.id) with nothing tying them
# together, so A could INSERT a row with owner_id=A (passes RLS's WITH CHECK, since it's A's
# own session) but job_id = B's job — a row visible to A that claims to be about B's job.
# mainai_jobs.UNIQUE(id, owner_id) + the child tables' composite FK closes exactly that gap:
# the (job_id, owner_id) pair itself now has to be a real row in mainai_jobs.


def test_mainai_job_events_composite_fk_rejects_owner_mismatch(db_session, superuser_db, make_verified_user):
    attacker, _ = make_verified_user()
    victim, _ = make_verified_user()
    victim_doc = _make_indexed_document(db_session, victim.id)
    _set_rls_user(db_session, victim.id)
    victim_job = service.create_job(
        db_session, owner_id=victim.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(victim_doc.id)}], created_by="founder"
    )

    # Attacker's own RLS context (owner_id=attacker passes WITH CHECK) but job_id points at
    # the victim's job — must be rejected by the composite FK, not silently accepted.
    _set_rls_user(db_session, attacker.id)
    with pytest.raises(Exception):
        db_session.execute(
            sa_text(
                "INSERT INTO mainai_job_events (id, job_id, owner_id, event_type, detail, created_at) "
                "VALUES (gen_random_uuid(), :job_id, :owner_id, 'created', '{}'::jsonb, now())"
            ),
            {"job_id": str(victim_job.id), "owner_id": str(attacker.id)},
        )
        db_session.commit()
    db_session.rollback()

    count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_events WHERE owner_id = :o"), {"o": str(attacker.id)}).scalar()
    assert count == 0


def test_mainai_job_events_composite_fk_accepts_matching_owner(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")

    db_session.execute(
        sa_text(
            "INSERT INTO mainai_job_events (id, job_id, owner_id, event_type, detail, created_at) "
            "VALUES (gen_random_uuid(), :job_id, :owner_id, 'heartbeat', '{}'::jsonb, now())"
        ),
        {"job_id": str(job.id), "owner_id": str(user.id)},
    )
    db_session.commit()

    count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_events WHERE job_id = :j AND event_type = 'heartbeat'"), {"j": str(job.id)}).scalar()
    assert count == 1


def test_mainai_job_proposals_composite_fk_rejects_owner_mismatch(db_session, superuser_db, make_verified_user):
    attacker, _ = make_verified_user()
    victim, _ = make_verified_user()
    victim_doc = _make_indexed_document(db_session, victim.id)
    _set_rls_user(db_session, victim.id)
    victim_job = service.create_job(
        db_session, owner_id=victim.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(victim_doc.id)}], created_by="founder"
    )

    _set_rls_user(db_session, attacker.id)
    with pytest.raises(Exception):
        db_session.execute(
            sa_text(
                "INSERT INTO mainai_job_proposals (id, job_id, owner_id, proposal_type, proposal_text, status, created_at) "
                "VALUES (gen_random_uuid(), :job_id, :owner_id, 'review_finding', 'planted', 'proposed', now())"
            ),
            {"job_id": str(victim_job.id), "owner_id": str(attacker.id)},
        )
        db_session.commit()
    db_session.rollback()

    count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE owner_id = :o"), {"o": str(attacker.id)}).scalar()
    assert count == 0


def test_mainai_job_proposals_composite_fk_accepts_matching_owner(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")

    db_session.execute(
        sa_text(
            "INSERT INTO mainai_job_proposals (id, job_id, owner_id, proposal_type, proposal_text, status, created_at) "
            "VALUES (gen_random_uuid(), :job_id, :owner_id, 'review_finding', 'legit', 'proposed', now())"
        ),
        {"job_id": str(job.id), "owner_id": str(user.id)},
    )
    db_session.commit()

    count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()
    assert count == 1


# --- K: DB-enforced append-only event log (migration 0026) -----------------------------------


def test_mainai_job_events_mainai_app_lacks_update_delete_privilege(db_session, superuser_db, make_verified_user):
    """mainai_app must not even reach the trigger for UPDATE/DELETE on mainai_job_events —
    the grant itself is gone (see migration 0026's REVOKE), not just trigger-blocked."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    event_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_events WHERE job_id = :j LIMIT 1"), {"j": str(job.id)}).scalar()

    with pytest.raises(Exception):
        db_session.execute(sa_text("UPDATE mainai_job_events SET event_type = 'heartbeat' WHERE id = :i"), {"i": str(event_id)})
        db_session.commit()
    db_session.rollback()

    with pytest.raises(Exception):
        db_session.execute(sa_text("DELETE FROM mainai_job_events WHERE id = :i"), {"i": str(event_id)})
        db_session.commit()
    db_session.rollback()

    row = superuser_db.execute(sa_text("SELECT event_type FROM mainai_job_events WHERE id = :i"), {"i": str(event_id)}).first()
    assert row is not None and row[0] == "created"


def test_mainai_job_events_trigger_denies_update_even_for_a_privileged_connection(db_session, superuser_db, make_verified_user):
    """The append-only guarantee isn't just a grant — the BEFORE UPDATE trigger denies it
    unconditionally, even for the superuser/migration connection (which otherwise bypasses
    RLS and holds every ordinary privilege)."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    event_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_events WHERE job_id = :j LIMIT 1"), {"j": str(job.id)}).scalar()

    with pytest.raises(Exception):
        superuser_db.execute(sa_text("UPDATE mainai_job_events SET event_type = 'heartbeat' WHERE id = :i"), {"i": str(event_id)})
        superuser_db.commit()
    superuser_db.rollback()


def test_mainai_job_events_trigger_denies_delete_without_the_erasure_flag(superuser_db, db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    event_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_events WHERE job_id = :j LIMIT 1"), {"j": str(job.id)}).scalar()

    with pytest.raises(Exception):
        superuser_db.execute(sa_text("DELETE FROM mainai_job_events WHERE id = :i"), {"i": str(event_id)})
        superuser_db.commit()
    superuser_db.rollback()

    row = superuser_db.execute(sa_text("SELECT 1 FROM mainai_job_events WHERE id = :i"), {"i": str(event_id)}).first()
    assert row is not None


# --- L: proposal immutability except the single proposed -> dismissed transition -------------


def test_mainai_job_proposals_allows_only_the_proposed_to_dismissed_transition(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=user.id, proposal_type="review_finding", proposal_text="original"))
    db_session.commit()
    proposal_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()

    db_session.execute(sa_text("UPDATE mainai_job_proposals SET status = 'dismissed' WHERE id = :i"), {"i": str(proposal_id)})
    db_session.commit()

    status = superuser_db.execute(sa_text("SELECT status FROM mainai_job_proposals WHERE id = :i"), {"i": str(proposal_id)}).scalar()
    assert status == "dismissed"


def test_mainai_job_proposals_rejects_the_reverse_transition(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=user.id, proposal_type="review_finding", proposal_text="original"))
    db_session.commit()
    proposal_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()
    db_session.execute(sa_text("UPDATE mainai_job_proposals SET status = 'dismissed' WHERE id = :i"), {"i": str(proposal_id)})
    db_session.commit()

    with pytest.raises(Exception):
        db_session.execute(sa_text("UPDATE mainai_job_proposals SET status = 'proposed' WHERE id = :i"), {"i": str(proposal_id)})
        db_session.commit()
    db_session.rollback()

    status = superuser_db.execute(sa_text("SELECT status FROM mainai_job_proposals WHERE id = :i"), {"i": str(proposal_id)}).scalar()
    assert status == "dismissed"


def test_mainai_job_proposals_rejects_editing_proposal_text(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=user.id, proposal_type="review_finding", proposal_text="original"))
    db_session.commit()
    proposal_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()

    with pytest.raises(Exception):
        db_session.execute(sa_text("UPDATE mainai_job_proposals SET proposal_text = 'tampered' WHERE id = :i"), {"i": str(proposal_id)})
        db_session.commit()
    db_session.rollback()

    text_value = superuser_db.execute(sa_text("SELECT proposal_text FROM mainai_job_proposals WHERE id = :i"), {"i": str(proposal_id)}).scalar()
    assert text_value == "original"


def test_mainai_job_proposals_mainai_app_lacks_delete_privilege(db_session, superuser_db, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=user.id, proposal_type="review_finding", proposal_text="x"))
    db_session.commit()
    proposal_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()

    with pytest.raises(Exception):
        db_session.execute(sa_text("DELETE FROM mainai_job_proposals WHERE id = :i"), {"i": str(proposal_id)})
        db_session.commit()
    db_session.rollback()

    row = superuser_db.execute(sa_text("SELECT 1 FROM mainai_job_proposals WHERE id = :i"), {"i": str(proposal_id)}).first()
    assert row is not None


# --- M: erase_own_mainai_job_children() — the only legitimate deletion path -------------------
#
# This function's FIRST draft (this same PR, before an independent security review) was named
# erase_mainai_job_children_for_owner(target_owner_id uuid), took the owner as a caller-
# supplied argument, and never checked it against app.current_user_id. Because the function
# is SECURITY DEFINER, its DELETEs ran with the function owner's privileges regardless of who
# called it or which owner_id they passed — RLS on the tables was NOT a sufficient boundary,
# and any authenticated session could have erased any other owner's event/proposal history by
# simply passing their uuid. The tests below prove the actual, fixed function: no argument
# exists to attack, the owner is derived from the calling session's own RLS-trusted GUC, and
# a session with no authenticated context at all is denied outright.


def test_erase_own_mainai_job_children_removes_only_the_calling_owners_rows(db_session, superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    other, _ = make_verified_user()
    owner_doc = _make_indexed_document(db_session, owner.id)
    other_doc = _make_indexed_document(db_session, other.id)
    _set_rls_user(db_session, owner.id)
    owner_job = service.create_job(db_session, owner_id=owner.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(owner_doc.id)}], created_by="founder")
    db_session.add(MainAIJobProposal(job_id=owner_job.id, owner_id=owner.id, proposal_type="review_finding", proposal_text="x"))
    db_session.commit()

    _set_rls_user(db_session, other.id)
    other_job = service.create_job(db_session, owner_id=other.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(other_doc.id)}], created_by="founder")
    db_session.add(MainAIJobProposal(job_id=other_job.id, owner_id=other.id, proposal_type="review_finding", proposal_text="y"))
    db_session.commit()

    _set_rls_user(db_session, owner.id)
    db_session.execute(sa_text("SELECT erase_own_mainai_job_children()"))
    db_session.commit()

    owner_events = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_events WHERE owner_id = :o"), {"o": str(owner.id)}).scalar()
    owner_proposals = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE owner_id = :o"), {"o": str(owner.id)}).scalar()
    other_events = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_events WHERE owner_id = :o"), {"o": str(other.id)}).scalar()
    other_proposals = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE owner_id = :o"), {"o": str(other.id)}).scalar()
    assert owner_events == 0
    assert owner_proposals == 0
    assert other_events > 0
    assert other_proposals == 1


def test_erase_own_mainai_job_children_has_no_owner_parameter_to_attack(superuser_db):
    """The vulnerable first draft took target_owner_id uuid. Proves that signature is gone
    entirely — not just unused — by checking pg_proc directly: exactly one overload, zero
    arguments. A caller literally cannot supply an owner id, attacker-controlled or otherwise."""
    row = superuser_db.execute(
        sa_text(
            "SELECT count(*), max(pronargs) FROM pg_proc "
            "WHERE proname = 'erase_own_mainai_job_children' AND pronamespace = 'public'::regnamespace"
        )
    ).first()
    overload_count, nargs = row
    assert overload_count == 1
    assert nargs == 0

    no_such_function = superuser_db.execute(
        sa_text("SELECT count(*) FROM pg_proc WHERE proname LIKE '%mainai_job_children_for_owner%' OR proname LIKE '%mainai_job_children_admin%'")
    ).scalar()
    assert no_such_function == 0, "the caller-supplied-owner-id function (vulnerable or an unreviewed admin variant) must not exist"


def test_erase_own_mainai_job_children_denies_a_session_with_no_auth_context(db_session, make_verified_user):
    """No app.current_user_id set at all (an unauthenticated/ambient connection) must be
    denied outright, not silently resolve to erasing nothing or, worse, NULL-matching rows."""
    from app.request_context import current_user_id as current_user_id_var

    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    db_session.commit()

    current_user_id_var.set(None)
    db_session.execute(sa_text("SET LOCAL app.current_user_id = ''"))
    with pytest.raises(Exception):
        db_session.execute(sa_text("SELECT erase_own_mainai_job_children()"))
        db_session.commit()
    db_session.rollback()


def test_mainai_app_can_execute_the_erasure_function(db_session, superuser_db, make_verified_user):
    """The function itself is what account.py's delete_account() calls, running as mainai_app
    — not the superuser/migration connection — so mainai_app must actually hold EXECUTE."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")

    db_session.execute(sa_text("SELECT erase_own_mainai_job_children()"))
    db_session.commit()

    remaining = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_events WHERE job_id = :j"), {"j": str(job.id)}).scalar()
    assert remaining == 0


def test_public_role_has_no_execute_on_any_mainai_job_function(superuser_db):
    rows = superuser_db.execute(
        sa_text(
            "SELECT routine_name FROM information_schema.routine_privileges "
            "WHERE routine_schema = 'public' AND privilege_type = 'EXECUTE' AND grantee = 'PUBLIC' "
            "AND routine_name IN ('erase_own_mainai_job_children', 'mainai_job_events_deny_mutation', 'mainai_job_proposals_guard_mutation')"
        )
    ).all()
    assert rows == []


def test_mainai_app_lacks_execute_on_the_trigger_functions(superuser_db):
    """Trigger functions are never meant to be called directly by application code — firing a
    trigger doesn't require EXECUTE on the calling role at all — so mainai_app holding EXECUTE
    on them would itself be a sign of drift."""
    for fn in ("mainai_job_events_deny_mutation", "mainai_job_proposals_guard_mutation"):
        rows = superuser_db.execute(
            sa_text(
                "SELECT 1 FROM information_schema.routine_privileges "
                "WHERE routine_schema = 'public' AND routine_name = :fn AND privilege_type = 'EXECUTE' AND grantee = 'mainai_app'"
            ),
            {"fn": fn},
        ).all()
        assert rows == [], f"mainai_app must not hold EXECUTE on trigger function {fn}"


def test_mainai_app_cannot_delete_events_even_with_the_erasure_flag_manually_set(db_session, superuser_db, make_verified_user):
    """The erasure GUC is not itself an authorization boundary — mainai_app must be denied by
    plain table privileges before the trigger's flag check is even reached. A session that
    manually sets the flag without going through erase_own_mainai_job_children() must still be
    unable to DELETE."""
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _set_rls_user(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    event_id = superuser_db.execute(sa_text("SELECT id FROM mainai_job_events WHERE job_id = :j LIMIT 1"), {"j": str(job.id)}).scalar()

    db_session.execute(sa_text("SET LOCAL app.mainai_job_erasure_in_progress = 'on'"))
    with pytest.raises(Exception):
        db_session.execute(sa_text("DELETE FROM mainai_job_events WHERE id = :i"), {"i": str(event_id)})
        db_session.commit()
    db_session.rollback()

    row = superuser_db.execute(sa_text("SELECT 1 FROM mainai_job_events WHERE id = :i"), {"i": str(event_id)}).first()
    assert row is not None


def test_apply_mainai_job_runtime_privileges_passes_against_the_real_migrated_state(superuser_db):
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)  # must not raise


def test_apply_mainai_job_runtime_privileges_detects_drift_and_rolls_back(superuser_db):
    """Proves the policy actually VERIFIES rather than just issuing REVOKE/GRANT and trusting
    them. Deliberately targets drift the enforce phase's own three static statements do NOT
    fix — a table-level over-grant (e.g. DELETE on mainai_job_events) would be self-healed by
    the enforce phase before verify ever ran, so that would never actually exercise the
    verify path. Granting EXECUTE on a TRIGGER function to mainai_app is different: nothing in
    the enforce phase ever touches function-level grants except the one legitimate GRANT
    EXECUTE on erase_own_mainai_job_children(), so this drift survives into the verify phase
    and must be caught there."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    superuser_db.execute(sa_text("GRANT EXECUTE ON FUNCTION mainai_job_events_deny_mutation() TO mainai_app"))
    superuser_db.commit()
    try:
        with pytest.raises(RuntimeError, match="mainai_job_events_deny_mutation"):
            apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)
    finally:
        superuser_db.execute(sa_text("REVOKE EXECUTE ON FUNCTION mainai_job_events_deny_mutation() FROM mainai_app"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_non_fatal_mode_warns_not_raises(superuser_db, caplog):
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    superuser_db.execute(sa_text("GRANT EXECUTE ON FUNCTION mainai_job_proposals_guard_mutation() TO mainai_app"))
    superuser_db.commit()
    try:
        apply_mainai_job_runtime_privileges(migration_engine, require_complete=False)  # must not raise
    finally:
        superuser_db.execute(sa_text("REVOKE EXECUTE ON FUNCTION mainai_job_proposals_guard_mutation() FROM mainai_app"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_detects_a_wrong_function_owner(superuser_db):
    """expected_owner is read from the real migration/admin connection's current_user, not
    hardcoded — a SECURITY DEFINER function owned by any OTHER real role, even one that is
    itself privileged (BYPASSRLS here, deliberately, to isolate this check from the separate
    owner-privilege check below), must be flagged: the whole point of a SECURITY DEFINER
    function is that its owner's identity is exactly who its DELETEs run as."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    role = f"pass17_wrong_owner_{uuid.uuid4().hex[:8]}"
    expected_owner = superuser_db.execute(sa_text("SELECT current_user")).scalar()
    superuser_db.execute(sa_text(f"CREATE ROLE {role} WITH BYPASSRLS NOSUPERUSER"))
    superuser_db.execute(sa_text(f"ALTER FUNCTION erase_own_mainai_job_children() OWNER TO {role}"))
    superuser_db.commit()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)
        assert "erase_own_mainai_job_children" in str(exc_info.value)
        assert "expected the migration/admin role" in str(exc_info.value)
    finally:
        superuser_db.execute(sa_text(f"ALTER FUNCTION erase_own_mainai_job_children() OWNER TO {expected_owner}"))
        superuser_db.execute(sa_text(f"DROP ROLE {role}"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_detects_an_owner_without_bypassrls_or_superuser(superuser_db):
    """A SECURITY DEFINER function meant to operate under FORCE RLS must be owned by a role
    that can actually do so — an owner with neither SUPERUSER nor BYPASSRLS is a sign of
    ownership drift even if its name happens to be unexpected in some other way too."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    role = f"pass17_weak_owner_{uuid.uuid4().hex[:8]}"
    expected_owner = superuser_db.execute(sa_text("SELECT current_user")).scalar()
    superuser_db.execute(sa_text(f"CREATE ROLE {role}"))  # neither SUPERUSER nor BYPASSRLS by default
    superuser_db.execute(sa_text(f"ALTER FUNCTION erase_own_mainai_job_children() OWNER TO {role}"))
    superuser_db.commit()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)
        assert "neither SUPERUSER nor BYPASSRLS" in str(exc_info.value)
    finally:
        superuser_db.execute(sa_text(f"ALTER FUNCTION erase_own_mainai_job_children() OWNER TO {expected_owner}"))
        superuser_db.execute(sa_text(f"DROP ROLE {role}"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_detects_mainai_app_as_table_owner(superuser_db):
    """Table ownership matters independently of function ownership: a SECURITY DEFINER
    function that deletes from these tables is only as trustworthy as the tables' own
    ownership — mainai_app owning the table it's restricted on would make the whole
    lockdown meaningless (an owner always has DDL rights over its own table)."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    expected_owner = superuser_db.execute(sa_text("SELECT current_user")).scalar()
    superuser_db.execute(sa_text("ALTER TABLE mainai_job_events OWNER TO mainai_app"))
    superuser_db.commit()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)
        assert "mainai_job_events" in str(exc_info.value)
        assert "must never be owned by mainai_app" in str(exc_info.value)
    finally:
        # ALTER TABLE ... OWNER TO rewrites relacl as a side effect (confirmed empirically:
        # ownership round-tripping through mainai_app wipes out its own SELECT/INSERT grants,
        # not just the privileges this test is deliberately probing) — re-grant ALL first,
        # mirroring scripts/ensure_app_role.py's real blanket boot-time grant, before letting
        # the policy narrow it back down, or this cleanup would leave mainai_app with zero
        # privileges on this table for the rest of the test session.
        superuser_db.execute(sa_text(f"ALTER TABLE mainai_job_events OWNER TO {expected_owner}"))
        superuser_db.execute(sa_text("GRANT ALL PRIVILEGES ON TABLE mainai_job_events TO mainai_app"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_detects_an_unexpected_overload(superuser_db):
    """pronargs alone can't distinguish an unexpected second overload from the one true
    zero-argument function — this proves a second overload (even with a harmless-looking
    single integer argument) is caught, not silently coexisting alongside the real one. Uses
    erase_own_mainai_job_children (return type void) rather than one of the trigger functions,
    since Postgres trigger functions can never have declared arguments at all."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    superuser_db.execute(
        sa_text(
            "CREATE FUNCTION erase_own_mainai_job_children(x integer) RETURNS void "
            "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$ BEGIN END; $$"
        )
    )
    superuser_db.execute(sa_text("REVOKE ALL ON FUNCTION erase_own_mainai_job_children(integer) FROM PUBLIC"))
    superuser_db.commit()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)
        assert "expected exactly 1 overload" in str(exc_info.value)
    finally:
        superuser_db.execute(sa_text("DROP FUNCTION erase_own_mainai_job_children(integer)"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_rolls_back_the_enforce_phase_too_on_failure(superuser_db):
    """A policy violation must roll back the WHOLE transaction, including the enforce phase's
    own REVOKE/GRANT statements that already ran earlier in the same transaction — not just
    leave the verify phase's read-only findings undone. Proven by manually re-granting an
    excess table privilege the enforce phase would normally revoke, ALSO injecting a
    verify-only violation that forces the policy to raise, then checking — from a separate
    connection — that the excess privilege is still there: if enforce's REVOKE had actually
    committed before the later verify failure, it wouldn't be."""
    from app.db import migration_engine
    from app.rls import apply_mainai_job_runtime_privileges

    superuser_db.execute(sa_text("GRANT DELETE ON mainai_job_events TO mainai_app"))
    superuser_db.execute(sa_text("GRANT EXECUTE ON FUNCTION mainai_job_proposals_guard_mutation() TO mainai_app"))
    superuser_db.commit()
    try:
        with pytest.raises(RuntimeError):
            apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)

        still_has_delete = superuser_db.execute(
            sa_text("SELECT has_table_privilege('mainai_app', 'public.mainai_job_events', 'DELETE')")
        ).scalar()
        assert still_has_delete is True, "enforce phase's REVOKE must roll back with the rest of the failed transaction"
    finally:
        superuser_db.execute(sa_text("REVOKE EXECUTE ON FUNCTION mainai_job_proposals_guard_mutation() FROM mainai_app"))
        superuser_db.commit()
        apply_mainai_job_runtime_privileges(migration_engine)


def test_apply_mainai_job_runtime_privileges_survives_reboots_blanket_grant_all(superuser_db):
    """scripts/ensure_app_role.py unconditionally re-grants ALL PRIVILEGES to mainai_app on
    every container boot, before this policy ever runs (see that script's docstring and the
    Pass 12 incident in docs/BRANCH_REGISTRY.md) — this proves the policy converges back to
    the exact intended privilege set even starting from that worst-case over-grant, matching
    the real boot order (ensure_app_role.py -> alembic -> apply_rls ->
    apply_mainai_job_runtime_privileges)."""
    from app.db import migration_engine
    from app.rls import (
        _MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES,
        _MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES,
        _effective_table_privileges,
        apply_mainai_job_runtime_privileges,
    )

    superuser_db.execute(sa_text("GRANT ALL PRIVILEGES ON TABLE mainai_job_events, mainai_job_proposals TO mainai_app"))
    superuser_db.commit()

    apply_mainai_job_runtime_privileges(migration_engine, require_complete=True)  # must not raise

    conn = superuser_db.connection()
    assert _effective_table_privileges(conn, "mainai_app", "mainai_job_events") == _MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES
    assert _effective_table_privileges(conn, "mainai_app", "mainai_job_proposals") == _MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES


# --- N: account erasure actually removes mainai job data ------------------------------------


def test_account_deletion_removes_mainai_job_data(client, db_session, superuser_db, make_verified_user):
    from app.founder import FOUNDER_USER_ID

    doc = _make_indexed_document(db_session, FOUNDER_USER_ID)
    _set_rls_user(db_session, FOUNDER_USER_ID)
    job = service.create_job(
        db_session, owner_id=FOUNDER_USER_ID, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder"
    )
    db_session.add(MainAIJobProposal(job_id=job.id, owner_id=FOUNDER_USER_ID, proposal_type="review_finding", proposal_text="x"))
    db_session.commit()

    csrf = _login(client)
    res = client.request("DELETE", "/api/account", json={"password": FOUNDER_PASSWORD}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text

    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)}).scalar() == 0
    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_events WHERE job_id = :j"), {"j": str(job.id)}).scalar() == 0
    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar() == 0


# --- O: lease fencing — founder re-review round (PR #36) --------------------------------------
# Reproduces, as a permanent regression test, the exact incident an independent founder review
# found and this session's own manual repro confirmed: a worker whose lease has already expired
# and been reclaimed by a second worker must be rejected by EVERY subsequent write it attempts
# -- not just future claims. Real separate sessions/connections throughout, matching this
# file's existing concurrency-test convention (see test_two_workers_racing_many_jobs_never_
# claim_the_same_job and test_owner_erasure_lock_serializes_erasure... in test_account_erasure.py).


def test_stale_worker_is_rejected_by_every_write_after_a_reclaim(db_session, superuser_db, make_verified_user):
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.rag.mainai_jobs_service import record_document_reviewed, record_document_skipped

    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    job_id = job.id

    admin = sessionmaker(bind=migration_engine)()
    try:
        _, _, generation_a = claim_next_mainai_job(admin, "worker-a", 120)
        # Simulate worker A stalling long enough for its lease to genuinely expire.
        admin.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": str(job_id)})
        admin.commit()
        claimed_b = claim_next_mainai_job(admin, "worker-b", 120)
        assert claimed_b is not None
        _, _, generation_b = claimed_b
        assert generation_b == generation_a + 1
    finally:
        admin.close()

    # Worker A -- unaware it lost the lease, exactly as corpus_review_job.py's real code path
    # behaves -- now attempts every kind of write this module fences. ALL must be rejected.
    _set_rls_user(db_session, user.id)
    stale_job = service.get_job(db_session, job_id)

    with pytest.raises(JobLeaseLostError):
        renew_mainai_job_lease(db_session, job_id, "worker-a", generation_a, 120)

    with pytest.raises(JobLeaseLostError):
        service.update_progress(db_session, stale_job, worker_id="worker-a", lease_generation=generation_a, current=1, total=1)

    with pytest.raises(JobLeaseLostError):
        record_document_reviewed(
            db_session, stale_job, worker_id="worker-a", lease_generation=generation_a,
            current=1, total=1, provider="openai", model="gpt-4o-mini",
            proposal_output_ref={"type": "mainai_job_proposal", "id": str(uuid.uuid4())},
        )

    with pytest.raises(JobLeaseLostError):
        record_document_skipped(db_session, stale_job, worker_id="worker-a", lease_generation=generation_a, document_id=doc.id, reason="unavailable")

    with pytest.raises(JobLeaseLostError):
        service.mark_completed(db_session, stale_job, worker_id="worker-a", lease_generation=generation_a)

    with pytest.raises(JobLeaseLostError):
        service.mark_failed(db_session, stale_job, worker_id="worker-a", lease_generation=generation_a, error_category=MainAIJobErrorCategory.unexpected)

    with pytest.raises(JobLeaseLostError):
        service.mark_cancelled(db_session, stale_job, worker_id="worker-a", lease_generation=generation_a)

    # None of worker A's rejected attempts may have left ANY trace.
    assert superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job_id)}).scalar() == 0
    row = superuser_db.execute(sa_text("SELECT status, locked_by, lease_generation FROM mainai_jobs WHERE id = :j"), {"j": str(job_id)}).first()
    assert row[0] == "running"
    assert row[1] == "worker-b"
    assert row[2] == generation_a + 1

    # Worker B, the legitimate current claimant, proceeds completely normally.
    fresh_job = service.get_job(db_session, job_id)
    _, _, generation_b = claimed_b
    service.update_progress(db_session, fresh_job, worker_id="worker-b", lease_generation=generation_b, current=0, total=1)
    db_session.commit()
    fresh_job = service.get_job(db_session, job_id)
    service.mark_completed(db_session, fresh_job, worker_id="worker-b", lease_generation=generation_b)

    final = superuser_db.execute(sa_text("SELECT status FROM mainai_jobs WHERE id = :j"), {"j": str(job_id)}).scalar()
    assert final == "completed"
    completed_events = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_job_events WHERE job_id = :j AND event_type = 'completed'"), {"j": str(job_id)}
    ).scalar()
    assert completed_events == 1, "exactly one completed event -- worker A's rejected attempt must not have added a second"


@pytest.mark.asyncio
async def test_run_corpus_review_job_stops_immediately_and_cleanly_when_its_lease_is_already_lost(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """The real entry point (not the individual service calls above): if run_corpus_review_job
    is somehow invoked with a stale (worker_id, lease_generation) -- e.g. a worker that was
    slow to even start after claiming -- its very first write (the initial update_progress
    call) must reject it, and the function must return cleanly with no further side effects,
    never raising out to the caller (app/worker.py catches JobLeaseLostError explicitly, but
    run_corpus_review_job itself is documented to swallow it and just stop)."""
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok())
    user, _ = make_verified_user()
    doc = _make_indexed_document(db_session, user.id)
    _make_chunk(db_session, user.id, doc.id)
    job = service.create_job(db_session, owner_id=user.id, job_type="corpus_review", input_refs=[{"type": "document", "id": str(doc.id)}], created_by="founder")
    _, _, real_generation = claim_next_mainai_job(superuser_db, "worker-real", 120)

    _set_rls_user(db_session, user.id)
    # A stale generation number for the SAME worker_id -- the exact scenario a hostname-reused
    # restarted worker process would hit.
    await run_corpus_review_job(db_session, job.id, user.id, worker_id="worker-real", lease_generation=real_generation + 999, lease_seconds=120)

    job = superuser_db.get(MainAIJob, job.id)
    assert job.status == MainAIJobStatus.running, "a stale-generation call must not have changed the job at all"
    proposal_count = superuser_db.execute(sa_text("SELECT count(*) FROM mainai_job_proposals WHERE job_id = :j"), {"j": str(job.id)}).scalar()
    assert proposal_count == 0


# --- P: rate limiting on cancel/retry (founder re-review round, PR #36, LOW item #8) ----------


def test_api_cancel_is_rate_limited_per_ip(client):
    """create_job already carried @limiter.limit — cancel/retry did not, so a caller could
    hammer either endpoint with unlimited requests (each one a real DB round-trip and, for
    retry, a real state-transition attempt) with no backpressure at all. Both now share the
    same rate_limit_default_per_minute budget create_job already used, verified here the same
    way tests/account/test_rate_limiting.py verifies other limiter-guarded endpoints: enough
    real requests to a real client to observe an actual 429, not a mocked limiter.

    The limit string is built once, at import time (`@limiter.limit(f"{settings...}/minute")`
    -- see app/routers/mainai_jobs.py), not re-read per request, so monkeypatching settings
    at test time would have no effect; this drives the real configured budget instead."""
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    unknown_job_id = uuid.uuid4()
    limit = get_settings().rate_limit_default_per_minute

    statuses = [client.post(f"/api/mainai/jobs/{unknown_job_id}/cancel", headers=headers).status_code for _ in range(limit + 5)]
    assert 429 in statuses


def test_api_retry_is_rate_limited_per_ip(client):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    unknown_job_id = uuid.uuid4()
    limit = get_settings().rate_limit_default_per_minute

    statuses = [client.post(f"/api/mainai/jobs/{unknown_job_id}/retry", headers=headers).status_code for _ in range(limit + 5)]
    assert 429 in statuses
