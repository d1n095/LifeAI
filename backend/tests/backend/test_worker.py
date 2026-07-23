"""Durable-worker package: tests for app/jobs/lease.py's claim/renew primitives and
app/worker.py's Worker — the restart-safe replacement for the old process-bound FastAPI
BackgroundTask (see app/worker.py's module docstring). Covers requirement 8's explicit
checklist items this file is responsible for: a running job with an expired lease is
reclaimed, two workers never process the same job simultaneously, and the worker's own claim
step works despite knowledge_import_jobs' per-owner RLS policy."""

import threading
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import migration_engine
from app.jobs.lease import claim_next_job, renew_lease
from app.models.import_job import ImportJob, ImportJobStatus

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

    async def _fake_embed(self, texts, model):
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
