import asyncio
import subprocess
from datetime import timedelta

import pytest

from app.mainai_execution.substrate import (
    Capability,
    CompletionEnvelope,
    DeterministicFakeProvider,
    DirectorContract,
    ExecutionSubstrate,
    LeaseLostError,
    ProviderState,
    SubstrateEventType,
    SubstrateError,
    completion_evidence,
)
from app.providers.base import Message, ProviderError


def _repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "state.txt").write_text("base")
    subprocess.run(["git", "-C", str(path), "add", "state.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def test_claim_fencing_abandonment_and_worktree_exclusivity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _repo(repo)
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    first = store.create_job(base_sha=base, worktree=str(repo))
    second = store.create_job(base_sha=base, worktree=str(repo))
    claim = store.claim(first, worker_id="worker-a", lease_seconds=1)
    with pytest.raises(SubstrateError, match="already claimed"):
        store.claim(second, worker_id="worker-b")
    future = __import__("datetime").datetime.now(__import__("datetime").timezone.utc) + timedelta(seconds=2)
    assert store.abandon_stale(now=future) == 1
    with pytest.raises(LeaseLostError):
        store.renew(claim, lease_seconds=1)
    store.claim(second, worker_id="worker-b")


def test_completion_rejects_unchanged_or_protected_and_accepts_actual_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _repo(repo)
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    job = store.create_job(base_sha=base, worktree=str(repo))
    claim = store.claim(job, worker_id="worker")
    with pytest.raises(SubstrateError):
        store.complete(claim, evidence=completion_evidence(claim))
    (repo / "state.txt").write_text("changed")
    subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "change"], check=True)
    evidence = completion_evidence(claim)
    store.complete(claim, evidence=evidence)


def test_provider_exhaustion_is_not_authorization_and_fake_is_deterministic(tmp_path):
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    snapshot = store.provider_state("fake", state=ProviderState.EXHAUSTED, authorized=False, remaining_units=0)
    assert snapshot.state is ProviderState.EXHAUSTED and snapshot.authorized is False
    provider = DeterministicFakeProvider(remaining_units=1)
    result = asyncio.run(provider.chat([Message("user", "hello")], "fake-model"))
    assert result.content == "fake:hello"
    with pytest.raises(ProviderError):
        asyncio.run(provider.chat([Message("user", "again")], "fake-model"))


def test_cancelled_claim_cannot_complete_and_heartbeat_nonce_is_explicit(tmp_path):
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    job = store.create_job(base_sha="a" * 40, worktree=None)
    claim = store.claim(job, worker_id="worker")
    store.heartbeat("worker", process_nonce="fresh-process", pid=123)
    assert store.stale_workers(timeout_seconds=60) == []
    store.request_cancel(job)
    with pytest.raises(LeaseLostError):
        store.renew(claim)


def test_director_contract_journal_and_exact_sha_completion(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    base = _repo(repo)
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    director = DirectorContract(store)
    submitted = director.submit_job(owner_id="owner-a", program="program-a", provider="fake", base_sha=base, worktree=str(repo), capabilities=(Capability.REPO_READ, Capability.CODE_EDIT))
    claim = director.claim_job(submitted.job_id, owner_id="owner-a", worker_id="worker-a")
    director.report_progress(claim, phase="editing", current=1)
    (repo / "state.txt").write_text("director result")
    subprocess.run(["git", "-C", str(repo), "add", "state.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "director"], check=True)
    evidence = completion_evidence(claim)
    event = director.report_completion(claim, CompletionEnvelope(evidence, "fake", evidence.observed_sha, ("state.txt",), ("pytest://fake",)))
    assert event.event_type is SubstrateEventType.JOB_COMPLETED
    assert [item.event_type for item in store.journal_events(job_id=submitted.job_id)] == [SubstrateEventType.JOB_ACCEPTED, SubstrateEventType.JOB_CLAIMED, SubstrateEventType.JOB_PROGRESS, SubstrateEventType.JOB_COMPLETED]


def test_late_attempt_and_reported_sha_mismatch_are_rejected(tmp_path):
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    director = DirectorContract(store)
    job = director.submit_job(owner_id="owner-a", program="program-a", provider="fake")
    old = director.claim_job(job.job_id, owner_id="owner-a", worker_id="one", lease_seconds=1)
    store.abandon_stale(now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc) + timedelta(seconds=2))
    director.substrate.retry_or_reassign(job.job_id, new_provider="fake")
    new = director.claim_job(job.job_id, owner_id="owner-a", worker_id="two")
    with pytest.raises(LeaseLostError):
        director.report_progress(old, phase="late", current=1)
    assert new.attempt_id != old.attempt_id


def test_director_owner_scope_and_backpressure(tmp_path):
    director = DirectorContract(ExecutionSubstrate(tmp_path / "state.sqlite"))
    first = director.submit_job(owner_id="alice", program="p", provider="fake", max_active=1)
    with pytest.raises(SubstrateError, match="backpressure"):
        director.submit_job(owner_id="alice", program="p", provider="fake", max_active=1)
    with pytest.raises(SubstrateError, match="owner scope"):
        director.claim_job(first.job_id, owner_id="bob", worker_id="w")


def test_journal_is_append_only_and_owner_scoped(tmp_path):
    store = ExecutionSubstrate(tmp_path / "state.sqlite")
    director = DirectorContract(store)
    director.submit_job(owner_id="alice", program="a")
    bob = director.submit_job(owner_id="bob", program="b")
    assert store.journal_events(owner_id="alice")
    assert all(event.job_id != bob.job_id for event in store.journal_events(owner_id="alice"))
    with store._connect() as db:
        with pytest.raises(__import__("sqlite3").IntegrityError, match="append-only"):
            db.execute("DELETE FROM recovery_journal")
