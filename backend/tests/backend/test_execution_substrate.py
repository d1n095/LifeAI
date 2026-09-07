import asyncio
import subprocess
from datetime import timedelta

import pytest

from app.mainai_execution.substrate import (
    DeterministicFakeProvider,
    ExecutionSubstrate,
    LeaseLostError,
    ProviderState,
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
