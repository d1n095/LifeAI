"""Durable-worker package: tests for app/jobs/lease.py's claim/renew primitives and
app/worker.py's Worker — the restart-safe replacement for the old process-bound FastAPI
BackgroundTask (see app/worker.py's module docstring). Covers requirement 8's explicit
checklist items this file is responsible for: a running job with an expired lease is
reclaimed, two workers never process the same job simultaneously, and the worker's own claim
step works despite knowledge_import_jobs' per-owner RLS policy."""

import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import SessionLocal, migration_engine
from app.jobs.lease import (
    ImportJobLeaseLostError,
    claim_next_job,
    configure_import_lease_fence,
    renew_lease,
)
from app.models.document import Document, DocumentSource
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
from app.storage import get_storage

_ClaimSession = sessionmaker(bind=migration_engine)


def _make_pending_job(superuser_db, owner_id, *, filename: str = "race.txt", checksum: str | None = None) -> ImportJob:
    job = ImportJob(
        owner_id=owner_id,
        status=ImportJobStatus.pending,
        source_filename=filename,
        source_checksum=checksum or uuid.uuid4().hex.ljust(64, "0"),
    )
    superuser_db.add(job)
    superuser_db.commit()
    superuser_db.refresh(job)
    return job


def test_claim_next_job_claims_a_pending_job_and_marks_it_running(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)

    claim_db = _ClaimSession()
    claimed = claim_next_job(claim_db, "worker-1", 120)
    claim_db.close()

    assert claimed == (job.id, user.id)
    superuser_db.expire_all()
    refreshed = superuser_db.get(ImportJob, job.id)
    assert refreshed.status == ImportJobStatus.running
    assert refreshed.locked_by == "worker-1"
    assert refreshed.lease_expires_at is not None
    assert refreshed.lease_expires_at > datetime.utcnow()
    assert refreshed.last_heartbeat_at is not None
    assert refreshed.started_at is not None


def test_claim_next_job_returns_none_when_nothing_is_claimable(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    _make_pending_job(superuser_db, user.id)
    claim_db = _ClaimSession()
    first = claim_next_job(claim_db, "worker-1", 120)
    second = claim_next_job(claim_db, "worker-1", 120)
    claim_db.close()

    assert first is not None
    assert second is None  # only one pending job existed


def test_claim_next_job_reclaims_a_job_whose_lease_has_expired(superuser_db, make_verified_user):
    """Simulates worker-restart recovery: a job left 'running' by a worker that crashed
    before its lease expired must become claimable again once the lease is past, without any
    manual intervention (see app/worker.py's module docstring)."""
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    job.status = ImportJobStatus.running
    job.locked_by = "dead-worker"
    job.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    job.last_heartbeat_at = datetime.utcnow() - timedelta(seconds=125)
    superuser_db.add(job)
    superuser_db.commit()

    claim_db = _ClaimSession()
    claimed = claim_next_job(claim_db, "worker-2", 120)
    claim_db.close()

    assert claimed == (job.id, user.id)
    superuser_db.expire_all()
    refreshed = superuser_db.get(ImportJob, job.id)
    assert refreshed.locked_by == "worker-2"
    assert refreshed.lease_expires_at > datetime.utcnow()


def test_claim_next_job_does_not_reclaim_a_job_with_a_still_valid_lease(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    job.status = ImportJobStatus.running
    job.locked_by = "worker-1"
    job.lease_expires_at = datetime.utcnow() + timedelta(seconds=120)
    job.last_heartbeat_at = datetime.utcnow()
    superuser_db.add(job)
    superuser_db.commit()

    claim_db = _ClaimSession()
    claimed = claim_next_job(claim_db, "worker-2", 120)
    claim_db.close()

    assert claimed is None


def test_renew_lease_extends_the_lease_only_for_the_owning_worker(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    claim_db = _ClaimSession()
    claim_next_job(claim_db, "worker-1", 5)

    ok = renew_lease(claim_db, job.id, "worker-1", 120)
    assert ok is True

    stolen = renew_lease(claim_db, job.id, "some-other-worker", 120)
    assert stolen is False
    claim_db.close()


def test_renew_lease_cannot_resurrect_an_expired_claim(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    expired_at = datetime.utcnow() - timedelta(seconds=5)
    job.status = ImportJobStatus.running
    job.locked_by = "stale-worker"
    job.lease_expires_at = expired_at
    superuser_db.commit()

    claim_db = _ClaimSession()
    try:
        assert renew_lease(claim_db, job.id, "stale-worker", 120) is False
    finally:
        claim_db.close()

    superuser_db.expire_all()
    refreshed = superuser_db.get(ImportJob, job.id)
    assert refreshed.locked_by == "stale-worker"
    assert refreshed.lease_expires_at == expired_at


def test_renew_lease_uses_wall_clock_after_transaction_has_been_open(
    superuser_db, make_verified_user
):
    """Reproduces transaction-time `now()` accepting a renewal after its real deadline."""
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    claim_db = _ClaimSession()
    try:
        transaction_started = claim_db.execute(text("SELECT now()")).scalar_one()
        superuser_db.execute(
            text("""
                UPDATE knowledge_import_jobs
                SET status = 'running',
                    locked_by = 'slow-transaction-worker',
                    lease_expires_at = clock_timestamp() + interval '150 milliseconds'
                WHERE id = :job_id
            """),
            {"job_id": str(job.id)},
        )
        superuser_db.commit()
        claim_db.execute(text("SELECT pg_sleep(0.3)"))

        assert renew_lease(claim_db, job.id, "slow-transaction-worker", 120) is False
        assert claim_db.execute(
            text("""
                SELECT lease_expires_at > :transaction_started
                   AND lease_expires_at < clock_timestamp()
                FROM knowledge_import_jobs WHERE id = :job_id
            """),
            {"job_id": str(job.id), "transaction_started": transaction_started},
        ).scalar_one()
    finally:
        claim_db.rollback()
        claim_db.close()


def test_concurrent_workers_reclaim_an_expired_job_once(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    superuser_db.execute(
        text("""
            UPDATE knowledge_import_jobs
            SET status = 'running',
                locked_by = 'expired-worker',
                lease_expires_at = clock_timestamp() - interval '1 second'
            WHERE id = :job_id
        """),
        {"job_id": str(job.id)},
    )
    superuser_db.commit()

    start = threading.Barrier(2)
    claimed: list[tuple[uuid.UUID, uuid.UUID] | None] = []

    def _claim(worker_id: str) -> None:
        claim_db = _ClaimSession()
        try:
            start.wait(timeout=5)
            claimed.append(claim_next_job(claim_db, worker_id, 120))
        finally:
            claim_db.close()

    threads = [
        threading.Thread(target=_claim, args=("reclaimer-a",)),
        threading.Thread(target=_claim, args=("reclaimer-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result is not None for result in claimed) == [False, True]
    superuser_db.expire_all()
    assert superuser_db.get(ImportJob, job.id).locked_by in {"reclaimer-a", "reclaimer-b"}


def test_stale_worker_cannot_commit_document_after_takeover(
    db_session, superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    claim_db = _ClaimSession()
    try:
        assert claim_next_job(claim_db, "stale-worker", 120) == (job.id, user.id)
        claim_db.execute(
            text("""
                UPDATE knowledge_import_jobs
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE id = :job_id
            """),
            {"job_id": str(job.id)},
        )
        claim_db.commit()
        assert claim_next_job(claim_db, "replacement-worker", 120) == (job.id, user.id)
    finally:
        claim_db.close()

    from app.request_context import current_user_id

    current_user_id.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :owner_id"), {"owner_id": str(user.id)})
    configure_import_lease_fence(db_session, job.id, user.id, "stale-worker", 120)
    db_session.add(
        Document(
            uploaded_by=user.id,
            title="stale-write.txt",
            source=DocumentSource.upload,
            checksum=uuid.uuid4().hex,
            media_type="text/plain",
            original_filename="stale-write.txt",
            import_job_id=job.id,
        )
    )
    with pytest.raises(ImportJobLeaseLostError):
        db_session.commit()
    db_session.rollback()

    superuser_db.expire_all()
    assert superuser_db.query(Document).filter_by(import_job_id=job.id).count() == 0
    assert superuser_db.get(ImportJob, job.id).locked_by == "replacement-worker"


def test_commit_fence_checks_expiry_after_waiting_for_job_row_lock(
    superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    claim_db = _ClaimSession()
    assert claim_next_job(claim_db, "fenced-worker", 1) == (job.id, user.id)
    claim_db.execute(
        text("""
            UPDATE knowledge_import_jobs
            SET lease_expires_at = clock_timestamp() + interval '1 second'
            WHERE id = :job_id
        """),
        {"job_id": str(job.id)},
    )
    claim_db.commit()
    claim_db.close()

    row_locked = threading.Event()
    release_lock = threading.Event()
    commit_started = threading.Event()
    outcome: list[str] = []
    errors: list[BaseException] = []
    backend_pid: list[int] = []

    def _hold_job_row() -> None:
        db = _ClaimSession()
        try:
            db.execute(
                text("""
                    SELECT id FROM knowledge_import_jobs
                    WHERE id = :job_id
                    FOR UPDATE
                """),
                {"job_id": str(job.id)},
            )
            row_locked.set()
            release_lock.wait(timeout=10)
        finally:
            db.rollback()
            db.close()

    def _commit_with_fence() -> None:
        from app.request_context import current_user_id

        db = SessionLocal()
        try:
            current_user_id.set(str(user.id))
            db.execute(
                text("SET LOCAL app.current_user_id = :owner_id"),
                {"owner_id": str(user.id)},
            )
            backend_pid.append(db.execute(text("SELECT pg_backend_pid()")).scalar_one())
            configure_import_lease_fence(db, job.id, user.id, "fenced-worker", 1)
            imported_job = db.get(ImportJob, job.id)
            imported_job.progress_current = 1
            commit_started.set()
            db.commit()
            outcome.append("committed")
        except ImportJobLeaseLostError:
            db.rollback()
            outcome.append("lease-lost")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the parent-thread assertion
            db.rollback()
            errors.append(exc)
        finally:
            db.close()

    blocker = threading.Thread(target=_hold_job_row)
    writer = threading.Thread(target=_commit_with_fence)
    blocker.start()
    assert row_locked.wait(timeout=5)
    writer.start()
    try:
        assert commit_started.wait(timeout=5), errors
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waiting = superuser_db.execute(
                text("""
                    SELECT wait_event_type = 'Lock'
                    FROM pg_stat_activity
                    WHERE pid = :pid
                """),
                {"pid": backend_pid[0]},
            ).scalar_one_or_none()
            if waiting:
                break
            time.sleep(0.02)
        assert waiting is True
        time.sleep(1.1)
    finally:
        release_lock.set()
        blocker.join(timeout=10)
        writer.join(timeout=10)

    assert not blocker.is_alive()
    assert not writer.is_alive()
    assert errors == []
    assert outcome == ["lease-lost"]
    superuser_db.expire_all()
    assert superuser_db.get(ImportJob, job.id).progress_current == 0


def test_cancelled_import_rejects_pending_worker_writes(
    db_session, superuser_db, make_verified_user
):
    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    claim_db = _ClaimSession()
    try:
        assert claim_next_job(claim_db, "cancelled-worker", 120) == (job.id, user.id)
        claim_db.execute(
            text("UPDATE knowledge_import_jobs SET status = 'cancelled' WHERE id = :job_id"),
            {"job_id": str(job.id)},
        )
        claim_db.commit()
    finally:
        claim_db.close()

    from app.request_context import current_user_id

    current_user_id.set(str(user.id))
    db_session.execute(text("SET LOCAL app.current_user_id = :owner_id"), {"owner_id": str(user.id)})
    configure_import_lease_fence(db_session, job.id, user.id, "cancelled-worker", 120)
    db_session.add(
        Document(
            uploaded_by=user.id,
            title="cancelled-write.txt",
            source=DocumentSource.upload,
            checksum=uuid.uuid4().hex,
            media_type="text/plain",
            original_filename="cancelled-write.txt",
            import_job_id=job.id,
        )
    )
    with pytest.raises(ImportJobLeaseLostError):
        db_session.commit()
    db_session.rollback()

    superuser_db.expire_all()
    assert superuser_db.query(Document).filter_by(import_job_id=job.id).count() == 0
    assert superuser_db.get(ImportJob, job.id).status == ImportJobStatus.cancelled


def test_import_claim_id_is_unique_even_for_same_process_and_worker_slot():
    import app.worker as worker_module

    first = worker_module._import_claim_id("stable-deployment-slot")
    reclaimed = worker_module._import_claim_id("stable-deployment-slot")

    assert first != reclaimed
    assert first.startswith("stable-deployment-slot:")
    assert len(first) <= 128


@pytest.mark.asyncio
async def test_stale_worker_cannot_record_failure_after_lease_loss(
    superuser_db, make_verified_user, monkeypatch
):
    from app.jobs.lease import ImportJobLeaseLostError
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    job = _make_pending_job(superuser_db, user.id)
    job.status = ImportJobStatus.running
    job.locked_by = "current-worker"
    job.lease_expires_at = datetime.utcnow() + timedelta(seconds=120)
    superuser_db.commit()

    async def _lost(*args, **kwargs):
        raise ImportJobLeaseLostError(job.id, "stale-worker")

    monkeypatch.setattr("app.worker.run_import_job", _lost)
    db = _ClaimSession()
    try:
        await process_claimed_job(db, job.id, user.id, "stale-worker")
    finally:
        db.close()

    superuser_db.expire_all()
    refreshed = superuser_db.get(ImportJob, job.id)
    assert refreshed.status == ImportJobStatus.running
    assert refreshed.locked_by == "current-worker"
    assert refreshed.failure_reason is None


def test_two_workers_racing_the_same_pending_jobs_never_claim_the_same_job(superuser_db, make_verified_user):
    """The actual FOR UPDATE SKIP LOCKED guarantee, exercised under real concurrency (two
    threads, two separate connections) rather than simulated — the mechanism this whole
    package's 'never indexed in parallel twice' requirement rests on."""
    user, _ = make_verified_user()
    job_ids = {_make_pending_job(superuser_db, user.id, filename=f"race-{i}.txt").id for i in range(20)}

    claimed_by: dict[str, list[uuid.UUID]] = {"worker-a": [], "worker-b": []}

    def _drain(worker_id: str) -> None:
        db = _ClaimSession()
        try:
            while True:
                result = claim_next_job(db, worker_id, 120)
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
    assert len(all_claimed) == len(job_ids), "every job must be claimed exactly once, none lost"
    assert len(set(all_claimed)) == len(all_claimed), "no job may be claimed by both workers"
    assert set(all_claimed) == job_ids


@pytest.mark.asyncio
async def test_worker_run_once_claims_and_processes_a_real_job_end_to_end(db_session, superuser_db, make_verified_user, monkeypatch):
    """Integration-level: Worker.run_once() (not claim_next_job directly) does the whole
    claim -> process -> terminal-status cycle a real poll iteration performs, proving the
    claim-session/process-session split in app/worker.py's run_once() actually works
    together, not just each half in isolation."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider
    from app.storage import get_storage
    from app.worker import Worker

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.02] * get_settings().embedding_dim for _ in texts]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    user, _ = make_verified_user()
    raw = b"Innehall som en riktig worker-cykel ska bearbeta."

    def _read_chunk():
        nonlocal raw
        chunk, raw = raw[:1 << 16], raw[1 << 16 :]
        return chunk

    blob = get_storage().write_stream(_read_chunk, max_bytes=1 << 16)
    job = ImportJob(
        owner_id=user.id,
        status=ImportJobStatus.pending,
        source_filename="worker-cycle.txt",
        source_checksum=blob.sha256,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
    )
    superuser_db.add(job)
    superuser_db.commit()
    job_id = job.id

    worked = await Worker().run_once()
    assert worked is True

    superuser_db.expire_all()
    refreshed = superuser_db.get(ImportJob, job_id)
    assert refreshed.status == ImportJobStatus.completed
    assert refreshed.succeeded_count == 1


def test_request_shutdown_stops_the_poll_loop_without_claiming_further_work(superuser_db, make_verified_user):
    """Graceful shutdown: once requested, run() must return promptly instead of polling
    forever, even with nothing claimable — the actual mechanism (an asyncio.Event checked at
    the top of every loop iteration and used as the sleep's own wait condition, see
    app/worker.py's run())."""
    import asyncio

    from app.worker import Worker

    async def _run_and_shutdown_immediately():
        worker = Worker()
        worker.request_shutdown()
        await asyncio.wait_for(worker.run(), timeout=5.0)

    asyncio.run(_run_and_shutdown_immediately())


def _store_a_real_blob(content: bytes) -> str:
    pos = 0

    def _read():
        nonlocal pos
        chunk = content[pos : pos + (1 << 16)]
        pos += len(chunk)
        return chunk

    return get_storage().write_stream(_read, max_bytes=max(len(content), 1)).storage_key


def test_worker_retry_storage_deletion_tasks_claims_and_purges_a_pending_task():
    """Pass 27: Worker._retry_storage_deletion_tasks now claims via claim_storage_deletion_
    tasks() (FOR UPDATE SKIP LOCKED, bounded batch) instead of a plain unlocked scan — this
    proves the real method still does its actual job end to end: a pending task with an
    unreferenced blob gets purged."""
    from app.worker import Worker

    storage_key = _store_a_real_blob(b"worker retry claim proof")
    claim_db = _ClaimSession()
    try:
        task = StorageDeletionTask(operation_id=uuid.uuid4(), storage_key=storage_key, status=StorageDeletionStatus.pending)
        claim_db.add(task)
        claim_db.commit()
        task_id = task.id

        Worker()._retry_storage_deletion_tasks(claim_db)

        claim_db.expire_all()
        refreshed = claim_db.get(StorageDeletionTask, task_id)
        assert refreshed.status == StorageDeletionStatus.purged
        assert not get_storage().exists(storage_key)
    finally:
        claim_db.rollback()
        claim_db.close()
